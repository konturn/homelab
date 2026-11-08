# Canary containers: boot-testing critical service changes before merge

A change to a critical service's config or image is booted in a throwaway
container before the MR can merge. The MR pipeline's `canary` job fails unless
the candidate comes up healthy by the service's own readiness checks and stays
up. Static checks (`nginx -t`, Home Assistant's `check_config`, a Ruby syntax
check) miss the failures that only show when the service starts.

Why this exists: on 2026-11-07, MR !1041 set `GITLAB_TRACING` in `gitlab.rb`.
Puma crash-looped on start (`NameError ::OpenTelemetry::Instrumentation::ActiveSupport`)
and GitLab was down for about 15 minutes until a manual revert. The change had
been tested in a standalone Ruby process and never by booting GitLab.

Covered today (`ci/canary/services.yml`): `gitlab`, `vault`, `homeassistant`,
`pihole`, `lab_nginx`, `nginx`, `iot_nginx`.

## How it works

```
MR pipeline (unprotected runner, br-ci, no Docker access)
  canary job: python3 ci/canary/client.py
     |  POST http://172.28.0.1:8743/run   header JOB-TOKEN: $CI_JOB_TOKEN
     v
homelab-canary.service (router host, ansible/roles/canary)
  1. GET /api/v4/job with the token -> must be a running job of project 4;
     the commit to test is that job's pipeline SHA, as GitLab reports it
  2. git fetch main + that SHA (same job token)
  3. canary.py plan: services whose config files or compose definition
     changed against merge-base(main, SHA)
  4. canary.py run, one service at a time (flock), output streamed back;
     last line CANARY-RESULT: PASS|FAIL
     v
Docker network "canary" (--internal, br-canary, 172.28.9.0/24)
  canary-<service>-<run>     candidate image + candidate config
  canary-<service>-<run>-*   throwaway sidecars (e.g. an OTLP collector)
```

For each service, `canary.py run`:

1. renders the candidate's `docker-compose.yml` and config files with the
   router's inventory variables. Rendering runs in a Jinja2 **sandbox**, and
   every undefined variable renders as `canary-dummy`, so every Vault secret
   is a dummy. Canaries never see a real secret;
2. builds the container from an **allowlist** of the candidate's compose
   definition: image, `cap_add`/`cap_drop`, `security_opt`, `user`,
   `ulimits`, `environment`, `entrypoint`/`command`, `tmpfs`, `read_only`,
   `healthcheck`. Ports, networks, volumes, devices, `container_name`, IPs,
   `privileged`, `network_mode`, `pid` and `extra_hosts` are never copied.
   Capabilities outside `ALLOWED_CAPS` and any `security_opt` other than
   `no-new-privileges` are refused. The image must belong to the service's
   `image_repos`;
3. creates fresh, empty host directories under
   `/var/lib/homelab-canary/runs/<run>/` for the service's data paths and
   writes the rendered files into them. Nothing from production is mounted.
   Where a service needs production state to boot meaningfully (Home
   Assistant's `automations.yaml`, `custom_components`, ...) it gets a
   **read-only copy**, and secret files contribute only their keys
   (`secrets.yaml` -> `key: canary-dummy`);
4. starts sidecars, then the service: `--cpu-shares 256` (cgroup weight 35,
   batch, CLAUDE.md "CPU priority"), a memory limit with no swap, a PID
   limit, `no-new-privileges`, `--restart no`;
5. waits until Docker reports `healthy` (the compose healthcheck, polled every
   15s) **and** every readiness probe in the spec passes, within `timeout`.
   An exited or OOM-killed container fails at once;
6. waits `stable_for` seconds and checks again (catches crash loops), then
   runs the one-shot `checks`;
7. collects `docker logs` and the spec's `log_files`, fails on any
   `fail_patterns` match, prints the last 200 lines of each and the peak
   memory (cgroup `memory.peak`);
8. tears everything down: containers, the network and the run directory. This
   happens in a `finally` block, also on SIGTERM (job canceled, service
   stopped). `canary.py gc` (run at service start and before every run) removes
   anything labelled `homelab.canary` that a crash left behind.

### Isolation

- **Network**: `canary` is a Docker `--internal` network with no gateway route.
  `rules.v4` also drops raw `PREROUTING` from `br-canary` to anything outside
  172.28.9.0/24, drops all `INPUT` from it, and drops `FORWARD` in and out of
  it ([FIREWALL.md](FIREWALL.md)). A canary reaches its own sidecars by
  Docker DNS alias and nothing else: no production service, no VLAN macvlan
  network, no internet. A canary therefore cannot register with or report to
  anything in production: GitLab runners, Vault, MQTT and the cloud
  integrations are all unreachable.
- **Names**: `canary-<service>-<run>`, hostname the same; production names and
  IPs are never reused. Where production config names another service, the
  canary either gets a throwaway sidecar under that name (`otel-collector`
  for GitLab) or, for nginx, the upstream names resolve to 127.0.0.1
  (`stub_upstream_hosts`).
- **Vault** uses its own empty file storage, has no unseal keys, and is
  initialised by the check with a one-key throwaway `vault operator init`.
- **Secrets**: dummies only (above). The job token travels over the local
  `br-ci` bridge to the router's own address; it is short-lived and valid only
  while the job runs.

### The service and the CI job

- `ansible/roles/canary` installs `canary.py`, `server.py` and
  `services.yml` from **main** to `/usr/local/lib/homelab-canary` and runs
  `homelab-canary.service`, listening on `172.28.0.1:8743` (the `br-ci`
  gateway). An MR cannot change how its own canary runs: it supplies only data
  (image reference, config files), which main's code renders in a sandbox and
  filters through the allowlist. A change to `ci/canary/` takes effect after it
  is merged and deployed (deploy scope: role `canary`).
- The `canary` job runs in MR pipelines when `docker/docker-compose.yml` or a
  covered service's config directory changes (`.gitlab-ci.yml`). It is not
  `allow_failure`, so it is required: a failing canary fails the pipeline and
  blocks the merge, for Renovate auto-merges too (the sweep merges only after
  a passed pipeline, [AUTOMERGE.md](AUTOMERGE.md)). When the change touches no
  covered service (e.g. another service's image in the compose file), the
  service answers `PASS (nothing to test)` within seconds.
- `resource_group: canary` keeps it to one canary job at a time; the service
  also serialises runs with a lock. Job timeout 60 min.
- Artifacts: `canary-logs/canary.log`, the whole streamed output (progress,
  tails of every log). The full logs of the last 30 runs stay on the router in
  `/var/lib/homelab-canary/logs/<job id>-<service>/`.
- Why not Docker access from CI: MR pipelines run on the unprotected runner,
  which has no Docker access by design, and the protected runner only takes
  jobs from protected refs. Even there, `docker-socket-proxy` refuses
  container, network and volume calls (`CONTAINERS=0`). The canary service is
  the one narrow door, and only for the fixed, allowlisted operation.
- Trust boundary: an MR controls its own `.gitlab-ci.yml`, so it could remove
  or fake the `canary` job. The job proves that a change that keeps the job
  boots, which covers Renovate and normal MRs. Reviewing changes to the job
  itself is a human's call.

### What a GitLab canary costs

A fresh omnibus boots from an empty data directory: reconfigure, schema load,
seed, then Puma and Sidekiq. The readiness probe also requires both to have
been up for 180s, and `stable_for` adds another 180s. Measured on the router
on 2026-11-08 (load ~20, 19.5.3, production gitlab.rb):

| Run | Result | Time | Peak memory |
|---|---|---|---|
| production gitlab.rb | PASS | 565s (ready after 376s) | 5.43 GiB (+0.05 GiB collector) |
| + two `gitlab-rails runner` processes | (held open by hand) | | 6.75 GiB |
| + `GITLAB_TRACING` (#106) | FAIL, container exited | 127s | 1.08 GiB |

The memory limit is 10 GiB. The others, same day: vault 79s / 0.15 GiB,
pihole 82s / 0.05 GiB, homeassistant 370s / 0.72 GiB (check_config retries
requirement installs that cannot reach PyPI), each nginx instance ~70s / 30 MB.

## Running one by hand

On the router (as root), from any checkout:

```bash
# the working tree as the candidate (no commit needed)
python3 /usr/local/lib/homelab-canary/canary.py run --worktree /root/repos/homelab \
    --service gitlab --out /tmp/canary-gitlab
# or a commit in a git dir
python3 /usr/local/lib/homelab-canary/canary.py run --git-dir /root/repos/homelab/.git \
    --rev <sha> --service homeassistant
# what an MR would test
python3 /usr/local/lib/homelab-canary/canary.py plan --git-dir /root/repos/homelab/.git \
    --rev <sha> --base origin/main
# remove leftovers (also runs automatically)
python3 /usr/local/lib/homelab-canary/canary.py gc
```

Exit 0 is pass. The run's own `ci/canary/canary.py` and `services.yml` from a
checkout work the same way, which is how to try a spec change before merging
it.

While a canary runs, `docker ps --filter label=homelab.canary` lists it, and
`docker exec -it canary-<service>-<run> sh` is fine for poking at it (e.g. the
`gitlab-rails runner` check in issue #106).

## Adding a service

Add an entry under `services:` in `ci/canary/services.yml` and the service's
config paths to the `canary` job's `changes:` in `.gitlab-ci.yml`. Usually a
dozen lines:

```yaml
  mosquitto:
    image_repos: [eclipse-mosquitto]       # the candidate may only name these
    triggers: [docker/mosquitto/**]        # plus the files below, plus its compose definition
    memory: 128m
    timeout: 120                           # seconds to ready
    mounts:                                # fresh, empty; nothing from production
      - {path: /mosquitto/config}
      - {path: /mosquitto/data}
    files:                                 # rendered with dummy secrets
      - {src: docker/mosquitto/mosquitto.conf, dest: /mosquitto/config/mosquitto.conf}
    readiness:                             # all must pass (plus the compose healthcheck)
      - name: broker answers
        exec: mosquitto_sub -h 127.0.0.1 -t '$SYS/broker/version' -C 1 -W 5
```

Then:

1. run it by hand against the current main (`--worktree`) and check it passes
   and that a deliberately broken config fails;
2. `python3 -m unittest discover -s ci/canary -p 'test_*.py'` (checks every
   spec's files, compose service and image repo exist, so a typo fails
   `canary:test`);
3. merge. The spec is live once `router:deploy` has run the `canary` role.

The other keys (`command`, `environment`, `seed`/`seed_production`,
`generate`, `stub_upstream_hosts`, `sidecars`, `checks` with `when`,
`log_files`, `fail_patterns`) are described at the top of `services.yml`;
the existing entries show each in use. Services that need hardware (USB
devices, the GPU) or a macvlan address to start at all are not good canary
candidates: the canary gets neither.

## When a canary fails

Open the job's `canary-logs/canary.log`: the line starting
`[canary ...] <service>: FAIL` gives the reason (exited, not ready within the
timeout with the last probe output, a crash loop, a failed check, or a fail
pattern with the matching line), followed by the tail of each log. Fix the
change; do not raise the timeout unless the log shows a slow but healthy boot.

If the canary service itself is broken (job says `unreachable` or
`refused`), check `systemctl status homelab-canary` and
`journalctl -u homelab-canary` on the router.
