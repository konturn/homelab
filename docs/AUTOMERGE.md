# Auto-merge for image updates

Routine container image updates (patch/minor releases and digest refreshes) for
a short list of stateless services can merge and deploy without a manual review.
Everything else goes through the normal review and full deploy.

Two classes share the policy below:

- **General**: any allowlisted service, digest/patch/minor. While the sweep
  schedule carries `AUTOMERGE_DRY_RUN=1` this class only logs what it would do.
- **Fast track**: digest refreshes and patch releases of the images of
  services reachable from outside the LAN, opened by Renovate. These merge as
  soon as their pipeline passes, also while the general class is in dry run.
  See [Fast track](#fast-track).

The pieces:

| Piece | Where | When |
|---|---|---|
| Policy + allowlist | `ci/automerge/policy.py`, `ci/automerge-allowlist.yml` | — |
| `automerge:eligibility` | MR pipeline | MRs touching `docker/docker-compose.yml` (informational only) |
| `automerge:sweep` | scheduled pipeline on `main`, `AUTOMERGE_TASK=sweep` | e.g. every 15 min |
| `router:deploy-service` | `main` pipeline | only for commits merged by the sweep |
| `automerge:report` | scheduled pipeline on `main`, `AUTOMERGE_TASK=report` | daily |

`ci/` is CODEOWNERS-protected, so the policy, allowlist and freeze file can only
change through a reviewed MR.

## Policy

An open MR is labelled `automerge-eligible` (and may be merged) only if **all** of
these hold:

1. The author is listed under `authors` in the allowlist (moltbot and the Renovate
   bot), the author is not the merging identity (`AUTOMERGE_TOKEN`'s user), and the
   target is `main`.
2. The only file changed is `docker/docker-compose.yml` (no adds, deletes, renames
   or mode changes).
3. Every changed line is a service's `image:` line, edited in place (same line
   count), and for each one:
   - the image name (registry/repository) is unchanged;
   - the new reference is pinned by `@sha256:` digest;
   - either only the digest changed (same tag), or the tag is a version upgrade
     within the same major version. For `0.x` versions the minor number must
     also stay the same (in semver a `0.x` minor bump may break). The tag's
     flavor must be unchanged (`-alpine`, `-ubuntu`, `-ls<build>` …). Tags that
     are not versions (`latest`, `nightly`) qualify only for digest-only updates.
4. Every changed service is listed under `services` in the allowlist, and the
   update kind (`digest`, `patch` or `minor`) is one it allows: all three by
   default, fewer if its entry sets `updates:`. For a fast-track image the kind
   must also be one that image allows (`digest`, `patch`, or `digest` only), on
   every service that uses it. Minor updates of fast-track images therefore
   always need a human, in every mode.
5. No CODEOWNERS-protected path is touched (the compose file is the one exception,
   and only under rule 3).
6. Nothing is frozen (see below).
7. Fewer than `max_per_24h` (default 3) general-class MRs, or fewer than
   `fast_track.max_per_24h` (default 4) fast-track MRs, carry the `automerged`
   label with a merge time in the last 24 hours. Each class has its own budget.
8. The router's ZFS pools still have redundancy. The gate looks at what each
   vdev can still lose, not at the `DEGRADED` label (`ci/automerge/policy.py`,
   "Host storage"; tests in `ci/automerge/test_storage.py` with `zpool status -p`
   fixtures):
   - **Every** auto-merge stops while a pool is `SUSPENDED`, `UNAVAIL` or
     `FAULTED` (any health other than `ONLINE`/`DEGRADED`), I/O is suspended,
     data errors are reported, or a data vdev has no redundancy left: a raidzN
     vdev with N members unavailable (`FAULTED`, `UNAVAIL`, `OFFLINE`,
     `REMOVED`), an N-way mirror with N-1, or a single-disk vdev. A
     `replacing`/`spare` slot counts as unavailable until its resilver has
     finished. Log, cache and spare vdevs do not count. Any error while
     checking counts as a failure.
   - While a resilver or scrub is running, **heavy changes** stop and normal
     image updates go ahead.
   - A `DEGRADED` pool that still has redundancy, with no resilver running, does
     not block. Error counters on devices that are still `ONLINE` do not block
     either; ZFS faults a device that keeps failing, and that then counts.

   A heavy change is a major-version upgrade of a stateful service: one listed
   under `stateful.services` in the allowlist, or one running a database image
   listed under `stateful.images`. It would need last night's backup to have
   succeeded and a fresh ZFS snapshot of the service's dataset. The bot cannot
   check either reliably, so heavy changes are **manual only** in every storage
   state; the sweep prints the reason on the MR's verdict (rule 3 refuses every
   major-version change in any case).

Before merging, the sweep also requires: the MR pipeline for the current head
commit succeeded, the MR is not a draft and has no conflicts. MRs labelled
`security` (set by the daily running-image CVE scan, or by Renovate for advisories)
are considered first. It merges at most one MR per run, through the API with the `sha` guard, so a push after evaluation
blocks the merge. The merge is done by a dedicated CI identity (`AUTOMERGE_TOKEN`),
never by the MR author. The merge commit message ends with the trailer
`Automerge-Policy: image-update`, which the `main` pipeline uses to select the
per-service deploy.

## Fast track

Purpose: when upstream rebuilds an image under the same tag (for example to pick
up a fixed base image or language runtime), the pinned digest keeps the old
build running until the digest MR merges. For services the internet can reach,
that window should be hours, not days.

The class is defined in `fast_track` in `ci/automerge-allowlist.yml`
(code-owned). An MR is fast-track only if, on top of every policy rule above:

1. `fast_track.enabled` is `true`;
2. the author is listed under `fast_track.authors` (the Renovate bot only:
   Renovate takes the new digest from the registry itself; moltbot's MRs stay
   in the general class);
3. the MR changes exactly **one image**, every changed line moving from the same
   old reference to the same new one (one or more services sharing that image);
4. that image is listed under `fast_track.images`, and the update is a kind it
   allows (`digest`/`patch` by default, `updates: [digest]` for some).

The sweep prints `class: fast track` or `class: general` for every eligible MR.
A fast-track merge gets the extra label `automerge-fast-track`, the trailer
`Automerge-Class: fast-track` in its merge commit, and the note says so.
`plan-deploy` re-checks the class from `main`'s policy before deploying a
commit whose MR carries that label.

### Which images

The services published through the external reverse proxy
(`docker/nginx/router.lab.nkontur.com/external_*.conf`) or a WAN port forward
(`networking/iptables/rules.v4`), with their update kinds:

| Image | Services | Kinds | Why |
|---|---|---|---|
| `nginx` | nginx, lab_nginx, iot_nginx, bluebubbles-relay | digest, patch | the external reverse proxy; the other three share the image, so Renovate changes them in the same MR |
| `plexinc/pms-docker` | plex | digest, patch | |
| `linuxserver/ombi` | ombi | digest, patch | |
| `homeassistant/home-assistant` | homeassistant | digest, patch | monthly releases (minor) migrate its database and stay reviewed |
| `lscr.io/linuxserver/deluge` | deluge | digest, patch | peer port forwarded through the VPN |
| `izderadicka/audioserve` | booksonic | digest | floating `latest` tag |
| `nextcloud` | nextcloud | digest | a new version runs `occ upgrade` on start |
| `vaultwarden/server` | bitwarden | digest | new versions migrate its database |
| `wordpress` | blog | digest | core version is pinned to the declared state |

Not fast-tracked:

- **Databases** (`nextcloud_db`, `wordpress_db`). They are reachable only from
  their application's internal network, restarting one interrupts the
  application, and a database image is the hardest to roll back. Their updates,
  digest refreshes included, stay on the reviewed path. The daily CVE scan still
  labels them `security` when the running image is affected.
- Locally built images (`jit-approval-svc` and others): Renovate does not track
  them.

When a service stops being reachable from outside (moved behind Tailscale,
converted to static files), remove its image from `fast_track.images` **and**
from the two "Fast track" rules in `renovate.json`.
`ci/automerge/test_fast_track.py` fails if the lists differ, if a fast-track
image is used by a service that is not allowlisted (Renovate updates every line
of an image in one MR, so a single unlisted service would make every such MR
ineligible), or if a database image is listed.

### Digest-only services and rollback

`nextcloud`, `bitwarden` and `blog` are allowlisted with `updates: [digest]`. A
digest refresh of the same tag keeps the application version, so the previous
image is a clean rollback. Version changes go through review and the full
deploy, which runs `occ upgrade` for Nextcloud. For Nextcloud,
`router-service-update.yml` also:

- **before** recreating, compares the version packaged in the new image
  (`/usr/src/nextcloud/version.php`) with the installed one
  (`/var/www/html/version.php`, a persistent volume) and refuses the deploy if
  they differ, so a same-tag rebuild that bumps the version cannot upgrade the
  installed code and database one way;
- **after** the health gate, requires `occ status` to report installed, not in
  maintenance mode, and no pending database upgrade.

Either failure takes the normal rollback path.

### Probes

Internal probes go to the internal reverse proxy (`lab_nginx_ip`). The exposed
services have no internal vhost, so their entries set `probe_via: external`: the
probe goes to the external reverse proxy (`nginx_ip`) with the public hostname,
which also checks the external nginx and TLS path end to end.

### Latency

Renovate runs hourly and proposes fast-track updates at any hour, outside its MR
limits ([RENOVATE.md](RENOVATE.md)). The MR pipeline then runs, and the sweep
(every 15 minutes, one merge per run) merges it. In total that is usually under
two hours after upstream publishes.

The `automerge:eligibility` job in MR pipelines runs checks 1–5 against the MR's
own checkout and prints the result. It has no credentials and decides nothing.

## Deploy and rollback

On a commit carrying the trailer, the full host deploys (`router:deploy`,
`zwave:deploy`, `satellite-2:deploy`, `macmini:deploy`) become manual jobs and
`router:deploy-service` runs instead (same `deploy-router` resource group):

1. `automerge.py plan-deploy` re-verifies the merge from `main`'s copy of the
   policy: the commit is a merge of an MR labelled `automerged` by an allowed
   author, merged by the auto-merge identity, and the first-parent diff is image lines only, for allowlisted services.
   If any check fails, nothing is deployed and the job fails; run the manual
   `router:deploy` if a full deploy is wanted.
2. `ansible/router-service-update.yml` fetches secrets, renders the compose file
   with the same task as the full deploy (`configure-homelab-services`
   `render_compose.yml`), then pulls and recreates **only** the changed services
   (`dependencies: false`, no orphan removal).
3. Health gate (`ci/automerge/wait-healthy.sh`, per service, up to `timeout`,
   default 600s). The container must run the new digest, its Docker healthcheck
   must report `healthy` (or, without one, it must stay up for 60s without
   restarting), and the optional `probe` URL must return 2xx/3xx. Probes go to
   the internal reverse proxy (`lab_nginx_ip`) with the URL's hostname, so they
   don't depend on DNS. If the container exits or restarts three times, the gate
   fails straight away.
   For Nextcloud, see [Digest-only services and rollback](#digest-only-services-and-rollback).
4. On failure the playbook renders the **pre-merge** compose file (the merge
   commit's first parent), recreates the same services on their previous image,
   runs the health gate again, and fails the job. Then `automerge.py deploy-report`:
   - opens (or updates) an issue labelled `automerge-rollback` with the failure and
     whether the rollback is healthy;
   - opens a revert MR (`automerge-revert/<iid>`) that restores the previous image
     lines on `main`;
   - labels the original MR `automerge-rollback`;
   - mails root (via the router's msmtp) with the same details.

Alerts:

- Rollback (health gate failed): mail from `deploy-report`, plus the issue.
- `plan-deploy` refused the merged commit (nothing deployed): mail from
  `automerge.py notify`.
- The job ended any other way without recording an outcome (timeout,
  cancellation, crash): mail from the job's `after_script`.
- A container still unhealthy afterwards also fires Grafana's
  `Container Health Check Failed` alert (email) after 10 minutes.

Rendering and recreating the previous image is idempotent, so you can retry the
job safely. Rollback restores the **image**, not data. A service whose upgrade
migrates its data on start may not run cleanly on the old image. Keep such
services off the allowlist.

While any `automerge-rollback` issue is open, auto-merge is paused for every MR.
`main` still references the new image until the revert MR is merged, so a full
router deploy in the meantime would apply it again. Merge (or close) the revert
MR, then close the issue.

## Freezing

Any one of these stops auto-merge:

- **One MR:** add the `freeze` label.
- **Everything, temporarily:** set the CI/CD variable `AUTOMERGE_FREEZE=true`
  (project settings, or on the pipeline schedule).
- **Everything, in git:** commit an `ci/automerge-freeze` file (any content) to `main`.
- An open `automerge-rollback` issue (automatic).

These apply to both classes. `AUTOMERGE_DRY_RUN=all` on the schedule also
stops both, but logs every decision. `fast_track.enabled: false` stops only
the fast track.

Removing the freeze needs no other action. The next sweep re-evaluates.

## Adding a service

Add it under `services` in `ci/automerge-allowlist.yml` (reviewed, since `ci/` is
code-owned). Use the compose **service name**, not the container name:

```yaml
services:
  myservice:
    probe: https://myservice.lab.nkontur.com/health   # optional, must return 2xx/3xx
    probe_via: external                              # optional: probe through the external proxy
    timeout: 900                                     # optional, seconds
    updates: [digest]                                # optional subset of [digest, patch, minor]
```

Only add services that are safe to restart on their own and that can go back to the
previous image without manual steps. That rules out databases and anything the
deploy itself depends on. A service that migrates data one way when its
*version* changes may be listed with `updates: [digest]`, because a same-tag
digest refresh keeps the version (see the fast track's digest-only services).
Without a container healthcheck or a probe, the only signal is "stays up
for 60s", so add a probe where an HTTP endpoint exists.

## Root-authored MRs

GitLab never lets an author approve their own MR. For a code-owned path whose
only owner is `@root`, a root-authored MR has no eligible approver, GitLab treats
that rule as invalid and it does not block. `docker/docker-compose.yml` also lists
the automerge bot, so there the rule stays valid and a root-authored MR (Noah, or
an agent working as root) needs the bot's approval.

After the MR's last push, once its pipeline has passed:

```bash
cd /root/repos/homelab   # on the router
scripts/approve-root-mr.sh <iid>
```

The script approves as the bot only if the author is root, the target is `main`,
the MR is open and not a draft, and the head pipeline for the current head commit
succeeded. The approval is pinned to that commit, and approvals reset on push, so
any later push needs a new run. It reads `AUTOMERGE_TOKEN` from the CI variable
and never prints it. Merge as usual afterwards.

The bot approves moltbot or Renovate MRs only through the sweep, under the policy
above. The script refuses any MR not authored by root.

## Setup

- `AUTOMERGE_TOKEN`: masked, protected CI/CD variable holding a **project access
  token** (`automerge-bot`, role Developer, scope `api`, expires yearly; the bot
  user is `project_4_bot_9927c1d39b8f3ba11b15bfa664d8b6d6`). It is never an MR
  author. It is listed as a second code owner of `docker/docker-compose.yml` so its
  approval satisfies the required code-owner approval on `main`. Besides the sweep,
  it is used only by `scripts/approve-root-mr.sh` (see "Root-authored MRs"). Rotate it by
  creating a new project access token with the same name/role/scope and updating
  the variable; the CODEOWNERS entry changes only if the bot user changes.
- Authors: `moltbot` and the Renovate bot (`RENOVATE_TOKEN`, see
  [RENOVATE.md](RENOVATE.md)). Renovate opens one MR per image, so a compose MR
  from it carries a single image change (possibly on several services that share
  that image).
- Pipeline schedules on `main` (owner `root`, timezone America/New_York):
  - `automerge sweep`: `*/15 * * * *`, variables `AUTOMERGE_TASK=sweep` and
    `AUTOMERGE_DRY_RUN=1`.
  - `automerge report`: `30 8 * * *`, variable `AUTOMERGE_TASK=report`.
- **Dry run**: while the sweep schedule has `AUTOMERGE_DRY_RUN=1`, the general
  class logs every decision (`ELIGIBLE` / `not eligible` with reasons, "would
  merge ...") and writes nothing. **The fast-track class stays live.**
  `AUTOMERGE_DRY_RUN=all` dry-runs both classes, and so does `sweep --dry-run`.
  To take the general class live, delete the variable from the schedule (Build >
  Pipeline schedules > `automerge sweep` > edit), or via the API:
  `glab api -X DELETE projects/4/pipeline_schedules/<id>/variables/AUTOMERGE_DRY_RUN`.
  To stop everything, set `AUTOMERGE_FREEZE=true` (or `AUTOMERGE_DRY_RUN=all`).
  To stop only the fast track, set `fast_track.enabled: false` in the allowlist
  (reviewed MR).
- Dry run from a checkout of `main`, with a read-only token:
  `GITLAB_TOKEN=... python3 ci/automerge/automerge.py evaluate <iid> [<iid> ...]`
- Tests: `python3 -m unittest discover -s ci/automerge -v` (`test_fast_track.py`
  also checks the real allowlist, `renovate.json` and compose file against each
  other).
