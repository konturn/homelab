# Host deploys (router, zwave, satellite-2, macmini)

Every push to `main` runs `router:deploy`, `zwave:deploy`, `satellite-2:deploy`
and `macmini:deploy`. Each one runs
`ci/deploy-scope/deploy_scope.py run --playbook <name>`, which runs
`ansible/<name>.yml` against its host, in full or limited to the roles whose
inputs changed.

## Changed-only deploys

1. **Base commit.** The job reads `/var/lib/homelab-deploy/<playbook>.sha` on
   the host. The last successful deploy of that playbook wrote it. The base is
   not `CI_COMMIT_BEFORE_SHA`: a deploy skipped by the resource group
   (`newest_ready_first`) or a failed deploy would then lose its changes. The
   marker is only written after `ansible-playbook` succeeds.
2. **Changed paths.** `git diff --name-only --no-renames <base> <commit>`. This
   is a tree diff, so merges, several merges in one push and force pushes all
   work.
3. **Rules.** Each path is matched against `ci/deploy-scope/scope.yml`, and the
   first matching rule decides:

   | order | rule | result |
   |---|---|---|
   | 1 | `shared.full`: `.gitlab-ci.yml`, `ci/deploy-scope/**`, `requirements.txt`, `ansible/requirements.yml`, `ansible/ansible.cfg`, `ansible/inventory.yml`, `ansible/known_hosts`, `ansible/{group,host}_vars/**`, `ansible/roles/fetch-vault-secrets/**`, `base/validate/**` | full deploy |
   | 2 | the playbook itself (`ansible/<playbook>.yml`) | full deploy |
   | 3 | `shared.none`: docs, READMEs, terraform, automerge/reports code, switch/AP config, other playbooks, ansible tests, unrelated scripts | no effect |
   | 4 | `ansible/roles/<role>/**` for a role the playbook lists | that role |
   | 5 | `playbooks.<playbook>` rules (repo files a role reads, see below) | the listed roles |
   | 6 | `ansible/roles/**` (roles this playbook does not use) | no effect |
   | 7 | anything else | **full deploy** |

4. **Run.**
   - full: `ansible-playbook ansible/<playbook>.yml`, as before;
   - some roles: `ansible-playbook ... --tags <role>,<role>`. Every role entry in
     the playbooks is tagged with its own name;
   - no roles: no ansible run ("nothing to deploy"). The marker still moves to
     the new commit.

   The job log prints every changed path, the rule that matched it and the
   final decision.

### Router path map (`playbooks.router` in scope.yml)

| paths | roles |
|---|---|
| `docker/docker-compose.yml` | configure-homelab-services, configure-wordpress |
| `docker/wordpress/**` | configure-wordpress |
| `docker/daemon.json` | configure-docker, configure-homelab-services |
| `docker/plex/convert_isos_makemkv.sh` | configure-router-network |
| satellite compose files, `docker/promtail/satellite-config.yml`, `docker/telegraf/satellite-telegraf.conf` | none |
| `docker/ollama/**` | configure-ollama-models |
| everything else under `docker/**` | configure-homelab-services |
| `base/nut/**`, `scripts/grafana-token-cleanup.sh` | configure-homelab-services |
| `base/disk/**`, `base/gitlab-runner/**`, `base/systemd/zpool-scrub.*`, `base/udev/**`, `networking/nfs/**` | configure-base |
| `base/msmtp/**` | configure-notifications |
| `networking/{avahi,dhcp,dns,iproute2,iptables,netplan,networkd,ssl}/**` | configure-router-network |

The satellites map their compose and config files to `deploy-compose`,
`base/minidsp` to `configure-zwave`, `snapclient/` to `configure-snapclient`,
`base/sane` and `shell2http.service` to `configure-scanner`, and
`docker/daemon.json` to `configure-docker` and `deploy-compose`. They ignore
the rest of `docker/`, `base/`, `networking/` and `scripts/`, which only
router roles read. The macmini playbook reads nothing outside its role.

## What always runs

- **fetch-vault-secrets** is tagged `always`. It runs on every deploy that
  runs any role and fails closed as before: a failed login, a failed read, a
  missing required path or a missing or empty required field stops the play
  before any role writes anything.
- The playbook `pre_tasks` (installed-package list) and fact gathering.
- Each role's own safety checks run whenever that role runs, because they
  are tasks of the role: `homelab-config-check` validation, `nginx -t`, the
  post-reload nginx check and restarts (configure-homelab-services), the
  dhcpd config check (configure-router-network), the LUKS keyfile and fstab
  guards (configure-base). A changed input selects its role, so those checks
  run for that change.

## Full deploys

A full deploy runs when:

- `DEPLOY_FULL=1` is set, for example from *Run pipeline* or with
  `git push -o ci.variable=DEPLOY_FULL=1`;
- the pipeline was not started by a push (schedule, *Run pipeline*, API).
  Set `DEPLOY_SCOPE=changed` to get a changed-only run from *Run pipeline*;
- the host has no marker, the marker is unreadable, or the base commit cannot
  be fetched;
- any changed path hits rule 1, 2 or 7;
- a rule names a role the playbook does not have.

**Nightly**: a pipeline schedule on `main` with `SCHEDULED_TASK=full-deploy`
runs a full deploy of every host. This corrects drift in roles that
changed-only deploys skipped. The schedule runs only the deploy jobs: the
other jobs skip scheduled task pipelines.

## Unreachable hosts

Before reading the marker, `deploy_scope.py run` opens a TCP connection to
port 22 of each host the playbook targets. If a host does not answer, the job
prints `=== host unreachable: ...`, runs nothing, leaves the marker alone and
exits **75**. Because the marker still points at the last successful deploy,
the first deploy after the host comes back diffs from there and applies every
change it missed.

`zwave:deploy` lists `allow_failure: exit_codes: [75]`: the Z-Wave Pi is on
the IoT VLAN and can be offline for hardware reasons, so an offline Pi shows
as a warning (the pipeline passes with warnings) and does not affect the other
hosts' deploys, which run in their own jobs anyway. Any other failure,
including a host that drops mid-run (Ansible's own exit code 4), still fails
the job. To give another host the same treatment, add the same
`allow_failure` to its deploy job (and to `test_ci_tolerates_unreachable_exit_code`).

## Image builds before deploys

The `<image>:build` jobs on `main` build on the router's Docker daemon and tag
`gitlab-registry.lab.nkontur.com:443/root/homelab/<image>:latest` there.
Compose records the image ID each container was created from and recreates a
container when the local image behind its tag has changed, so the deploy that
follows a build picks up the new image without a pull. That only works if the
deploy starts after the build has finished.

The deploy jobs use `needs:`, so stage order does not hold them back. Instead
`router:deploy` and `router:deploy-service` list every build of an image in
`docker/docker-compose.yml` with `optional: true`. On `main` each build is
either path-triggered, and the deploy waits for it, or a manual job that is
allowed to fail, and the deploy does not wait for it. A failed build stops
the router deploy, so the host never runs a half-updated stack. The other
host deploys use no registry images and wait for no build.

`test_deploy_needs.py` (run by `deploy-scope:test`) checks that every
registry image in the router compose file has a `.docker_build` job, that
both router deploy jobs need all of those builds (optionally) and no others,
and that no other compose file uses registry images. When you add an image,
add its build job to the `needs:` of both router deploy jobs.

## Keeping scope.yml correct

`deploy-scope:test` (MR and main, whenever ansible/, docker/, base/,
networking/, snapclient/, scripts/ or the CI file change) checks:

- sample diffs: docs only, a single service, dhcpd, a shared file and an
  unmapped path each give the expected decision;
- every repo file a role reads through `lookup('env', 'CI_PROJECT_DIR')`
  maps to that role, or to full, in every playbook that uses the role;
- every inventory `docker_config` src, compose file and nginx config of each
  host maps to `configure-homelab-services` (router) or `deploy-compose`
  (satellites);
- every role entry in the playbooks is tagged with its name, no role task
  uses the `never` tag, and every role named in scope.yml exists in its
  playbook.

When you add a repo file that a role reads, add it to scope.yml. If you
forget, the path is unmapped and triggers a full deploy, unless it falls
under a broader rule; the test catches literal paths and inventory entries.
A new role must be tagged with its name in the playbook.

Try a diff locally:

```sh
python3 ci/deploy-scope/deploy_scope.py select --playbook router --base origin/main~3
python3 ci/deploy-scope/deploy_scope.py select --playbook zwave docker/daemon.json
```

## SSH host keys

Every CI job that SSHes to a host checks its host key strictly against
`ansible/known_hosts`: Ansible (`ANSIBLE_HOST_KEY_CHECKING=True`, and
`ansible_ssh_common_args` in `inventory.yml` sets `StrictHostKeyChecking=yes`
with `UserKnownHostsFile` pointing at the repo file), and the direct `ssh`
calls in `.gitlab-ci.yml`, `ci/` and `scripts/gitlab-settings.sh`. Do not turn
this off (`StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`,
`host_key_checking = False`); fix the pinned key instead.

The file lists each host under its inventory name and its `ansible_host`
address, which is what CI connects to. When a host is rebuilt, re-imaged or
its SSH keys change, deploys to it fail with "Host key verification failed"
until the file is updated:

1. Read the new public keys on the host itself, over a session you already
   trust (console, or an SSH session whose key you verified):
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` (also ecdsa and rsa).
2. Scan from inside the network and compare fingerprints with step 1:
   `ssh-keyscan -t ed25519,ecdsa,rsa <ip> | ssh-keygen -lf -`.
   Only continue if they match.
3. Replace that host's lines in `ansible/known_hosts`
   (`<name>,<ip> <type> <key>`), and add a line for any new name or address
   CI uses. Remove lines for hosts CI no longer reaches.
4. Check it: `ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=ansible/known_hosts root@<ip> true`.

`ansible/known_hosts` is in `shared.full`, so merging the change runs a full
deploy of every host.

## Timing

`profile_tasks` prints the 20 slowest tasks at the end of every run.

Full deploys, job duration (2026-10-21):

| job | before (job 25100 etc.) | after !846, !849, !850 |
|---|---|---|
| router:deploy | 366s | 222s |
| zwave:deploy | 216s | 124s |
| satellite-2:deploy | 195s | 104s |
| macmini:deploy | 40s | 28s |

Most of the gain came from collapsing per-item remote loops (Vault reads,
config files, symlinks) and skipping apt calls when packages are present.
