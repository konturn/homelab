# Auto-merge for image updates

Container image updates in the deployed compose files merge and deploy
without a manual review: digest refreshes, digest pins, and patch and minor
releases, for every service, unless one of the exceptions below applies. Each
merge deploys only the changed services, waits for them to become healthy,
and rolls back automatically if they do not.

What stays with a human:

- **Major versions**, and tag changes whose version scheme is ambiguous
  (treated as major). See [Major versions](#major-versions).
- **The denylist** in `ci/automerge-policy.yml`: GitLab and what the CI and
  deploy path needs to run. See [Denylist](#denylist).
- **Services under a compose profile** (the retired `wordpress` profile: `blog`,
  `wordpress_db`). A per-service deploy names the service, which starts it
  whatever its profiles.
- **Restricted kinds**: a service whose version change runs a one-way
  migration lists only the kinds that roll back cleanly (`updates:`).
- **Anything that is not an image line in a deployed compose file**:
  Dockerfiles, CI job images in `.gitlab-ci.yml`, Python/npm dependencies,
  Terraform providers. These have no per-service deploy and health gate.

The sweep labels such MRs `automerge-manual` and notes the reason on the MR
(see [Visibility](#visibility-of-manual-updates)).

Two classes share the policy:

- **General**: any eligible update. While the sweep schedule carries
  `AUTOMERGE_DRY_RUN=1` this class only logs what it would do.
- **Fast track**: digest refreshes and patch releases of the images of
  services reachable from outside the LAN, opened by Renovate. They are
  proposed at any hour and merge as soon as their pipeline passes, also while
  the general class is in dry run. See [Fast track](#fast-track).

The pieces:

| Piece | Where | When |
|---|---|---|
| Policy | `ci/automerge/policy.py`, `ci/automerge-policy.yml` | — |
| Hosts per compose file | `docker_compose_file_names` in `ansible/inventory.yml` | — |
| `automerge:eligibility` | MR pipeline | MRs touching `docker/docker-compose*.yml` (informational only) |
| `automerge:sweep` | scheduled pipeline on `main`, `AUTOMERGE_TASK=sweep` | every 15 min |
| `<host>:deploy-service` (`router`, `zwave`, `satellite-2`) | `main` pipeline | only for commits merged by the sweep |
| `automerge:report` | scheduled pipeline on `main`, `AUTOMERGE_TASK=report` | daily |

`ci/` is CODEOWNERS-protected, so the policy and freeze file can only change
through a reviewed MR.

## Policy

An open MR is labelled `automerge-eligible` (and may be merged) only if **all** of
these hold:

1. The author is listed under `authors` in the policy file (moltbot and the
   Renovate bot), the author is not the merging identity (`AUTOMERGE_TOKEN`'s
   user), and the target is `main`.
2. Every file changed is listed under `compose_files` (no adds, deletes,
   renames or mode changes). A compose file is listed only if a host renders
   it (`docker_compose_file_names` in the inventory) and every such host has a
   per-service deploy (`deploy_hosts`). Today:

   | Compose file | Hosts | Deploy job |
   |---|---|---|
   | `docker/docker-compose.yml` | router | `router:deploy-service` |
   | `docker/docker-compose-observability.yml` | zwave, satellite-2 | `zwave:deploy-service`, `satellite-2:deploy-service` |
   | `docker/docker-compose-zwave.yml` | zwave | `zwave:deploy-service` |

   `docker-compose-zigbee.yml` and `docker-compose-satellite.yml` are rendered
   by no host, so Renovate ignores them and the sweep refuses them.
3. Every changed line is a service's `image:` line, edited in place (same line
   count), and for each one:
   - the image name (registry/repository) is unchanged;
   - the new reference is pinned by `@sha256:` digest;
   - either only the digest changed (same tag; this includes pinning a digest),
     or the tag is a version upgrade within the same major version. See
     [Major versions](#major-versions).
4. No changed service or image is on the denylist, no changed service has
   compose `profiles` (in either version of the file), and the update kind
   (`digest`, `patch` or `minor`) is one the service allows: all three unless
   its entry under `services` sets `updates:`.
5. No CODEOWNERS-protected path is touched (the listed compose files are the
   exception, and only under rule 3).
6. Nothing is frozen (see below) and the MR is not labelled `major`.
7. Fewer than `max_per_24h` (12) general-class MRs, or fewer than
   `fast_track.max_per_24h` (4) fast-track MRs, carry the `automerged` label
   with a merge time in the last 24 hours. Each class has its own budget.

A malformed `denylist` or `fast_track` section refuses every MR (fails closed).

Before merging, the sweep also requires:

- the MR pipeline for the current head commit succeeded;
- the MR is not a draft and has no conflicts;
- no per-service deploy job is still created, pending or running, so a failing
  deploy opens its rollback issue before the next update lands;
- no open `automerge-rollback` issue covers a service the MR updates on the
  same host. The hold is per service: other services keep auto-merging, since
  their per-service deploys recreate only their own containers. The issue body
  carries the scope (`<!-- automerge-rollback job=router services=tautulli -->`);
  an older issue without it is read from its title, and one that matches
  neither holds every MR;
- for an MR that deploys to a Pi (zwave, satellite-2), the Pi answers on
  port 22.

MRs labelled `security` (set by the daily running-image CVE scan, or by
Renovate for advisories) are considered first. It merges at most one MR per
run (a dry-run "would merge" does not count), through the API with the `sha`
guard, so a push after evaluation blocks the merge. The merge is done by a
dedicated CI identity (`AUTOMERGE_TOKEN`), never by the MR author. The merge
commit message ends with these trailers:

```
Automerge-Deploy: [zwave] [satellite-2]
Automerge-Policy: image-update
```

`Automerge-Policy` makes the full host deploys manual for that commit;
`Automerge-Deploy` selects the per-service deploy jobs. Each of those
re-derives the hosts from `main`'s policy and inventory and refuses to deploy
if its host is not one of them.

### Major versions

`classify_tag_change` accepts only tags of the form `major.minor[.patch[.build]]`
plus an unchanged flavor suffix (`-alpine`, `-ubuntu`, `-ls<build>`, a trailing
build hash). Everything else counts as major and is never merged:

- a different major number; for `0.x` versions a different minor (in semver a
  `0.x` minor bump may break);
- a tag that is not such a version (`latest`, `nightly`, `18993091-ls1`, a date
  like `20261101`): these qualify for digest-only updates of the same tag only;
- a changed flavor (`-alpine` to `-bookworm`);
- calendar versions follow the same rule: `2026.10.x` to `2026.11.0` is a minor
  update, `2026.12.x` to `2027.1.0` a major one.

Renovate labels major updates `major` (and holds them for approval on the
dependency dashboard). The sweep refuses any MR with that label as well, so
both version schemes must agree that an update is not major.

A heavy change is a major-version upgrade of a stateful service: one listed
under `stateful.services` in the policy file, or one running a database image
listed under `stateful.images`. It would need last night's backup to have
succeeded and a fresh ZFS snapshot of the service's dataset. The bot cannot
check either reliably, so the sweep prints that reason on the verdict too. The
sweep does not check the state of the ZFS pools.

### Denylist

`denylist` in `ci/automerge-policy.yml` names services and images that are
never auto-merged, each with its reason:

| Entry | Reason |
|---|---|
| `gitlab` (`gitlab/gitlab-ee`, `gitlab/gitlab-ce`) | GitLab runs the pipelines, the sweep and the per-service deploy. A bad upgrade takes down the pipeline that would roll it back, and its database migrations are one-way. |
| `gitlab/gitlab-runner` | Runs every CI job. Today the runner is a host package (`configure-base`), not a compose service; the entry covers a future compose runner. |
| `vault` (`hashicorp/vault`) | Every deploy job reads its secrets from Vault before it changes anything. A Vault that breaks after the health gate blocks every later deploy, the revert included. |
| `docker-socket-proxy` (`tecnativa/docker-socket-proxy`) | CI image builds reach the Docker daemon through it. |
| `headway-frontend-init`, `headway-travelmux-init`, `pelias-*-init` | One-shot init containers: they exit by design, which the health gate cannot tell from a failure. (Renovate also ignores their images.) |

Renovate labels updates of the denylisted images `automerge-manual` and adds a
note to the MR body (`renovate.json`, rule "Auto-merge denylist").
`test_policy_scope.py` checks that the two lists match.

### GitLab updates

GitLab's own updates are never auto-merged. Renovate opens them with the
`automerge-manual` label, and they stay listed in the daily report until
someone merges or closes them. A checklist for merging one by hand:

1. Read the release notes for required upgrade stops, and check that no
   background migrations are running (Admin > Monitoring > Background
   migrations).
2. Check that last night's backup succeeded.
3. Merge the MR. The commit has no `Automerge-Policy` trailer, so the normal
   `router:deploy` recreates the `gitlab` container, from inside a pipeline
   that GitLab itself runs. That job can lose contact with GitLab while it
   restarts.
4. Check that `gitlab` reports `healthy` in `docker ps` on the router and that
   a new pipeline runs. If GitLab does not come back, no pipeline can deploy a
   revert: put the previous image line back in the rendered compose file on
   the router (`docker_compose_dest_path`) and recreate the container by hand,
   then revert the MR once GitLab is up.

### Restricted kinds

| Service | `updates` | Why |
|---|---|---|
| `nextcloud`, `bitwarden` | digest | A new version runs a one-way migration on start (`occ upgrade`, Vaultwarden's schema). |
| `homeassistant` | digest, patch | Monthly (minor) releases migrate its recorder database. |
| `influxdb` | digest, patch | 2.x minor releases migrate its metadata store one way (`influxd downgrade` to go back). |

Their other non-major updates wait for a human, who merges them through the
normal review and full deploy.

### Visibility of manual updates

An update MR (Renovate's, or a compose image change by moltbot) that the
policy leaves to a human gets the label `automerge-manual` and one note
listing the reasons, the first time the sweep sees it. It also appears:

- on the Renovate dependency dashboard (open MRs, or pending approval for
  majors);
- in the daily `automerge:report` mail under "Updates waiting for a human",
  with its age. A waiting MR labelled `security` makes the report mail even on
  a day with nothing merged.

The label is removed again if the MR becomes eligible.

## Fast track

Purpose: when upstream rebuilds an image under the same tag (for example to pick
up a fixed base image or language runtime), the pinned digest keeps the old
build running until the digest MR merges. For services the internet can reach,
that window should be hours, not days.

The class is defined in `fast_track` in `ci/automerge-policy.yml`
(code-owned). An MR is fast-track only if, on top of every policy rule above:

1. `fast_track.enabled` is `true`;
2. the author is listed under `fast_track.authors` (the Renovate bot only:
   Renovate takes the new digest from the registry itself; moltbot's MRs stay
   in the general class);
3. the MR changes exactly **one image** in `docker/docker-compose.yml`, every
   changed line moving from the same old reference to the same new one (one
   or more services sharing that image);
4. that image is listed under `fast_track.images`, and the update is a kind it
   allows (`digest`/`patch` by default, `updates: [digest]` for some).

Other non-major updates of these images (minor releases) go through the
general class, if the service's `updates` allows them.

The sweep prints `class: fast track` or `class: general` for every eligible MR.
A fast-track merge gets the extra label `automerge-fast-track`, the trailer
`Automerge-Class: fast-track` in its merge commit, and the note says so.
`plan-deploy` re-checks the class from `main`'s policy before deploying a
commit whose MR carries that label.

### Which images

The services published through the external reverse proxy
(`docker/nginx/router.lab.nkontur.com/external_*.conf`) or a WAN port forward
(`networking/iptables/rules.v4`), with their fast-tracked kinds:

| Image | Services | Kinds | Note |
|---|---|---|---|
| `plexinc/pms-docker` | plex | digest, patch | |
| `linuxserver/ombi` | ombi | digest, patch | |
| `homeassistant/home-assistant` | homeassistant | digest, patch | minors stay reviewed (restricted kind) |
| `lscr.io/linuxserver/deluge` | deluge | digest, patch | peer port forwarded through the VPN |
| `izderadicka/audioserve` | booksonic | digest | floating `latest` tag |
| `nextcloud` | nextcloud | digest | a new version runs `occ upgrade` on start |
| `vaultwarden/server` | bitwarden | digest | new versions migrate its database |

Not fast-tracked:

- **Reverse proxies** (nginx, lab_nginx, iot_nginx, bluebubbles-relay). They
  run the local build from `docker/images/nginx`; its base image updates are
  Dockerfile changes, which always need review.
- **Databases** (`nextcloud_db`, `influxdb`, `paperless-redis`). A database
  image is the hardest to roll back; their non-major updates go through the
  general class.
- Locally built images (`jit-approval-svc` and others): Renovate does not track
  them.

When a service stops being reachable from outside (moved behind Tailscale,
converted to static files), remove its image from `fast_track.images` **and**
from the two "Fast track" rules in `renovate.json`.
`ci/automerge/test_fast_track.py` fails if the lists differ, if a fast-track
image is used by a denylisted or profile-gated service (Renovate updates every
line of an image in one MR, so one such service would make every such MR
ineligible), or if a database image is listed.

### Digest-only services and rollback

`nextcloud` and `bitwarden` have `updates: [digest]`. A digest refresh of the
same tag keeps the application version, so the previous image is a clean
rollback. Version changes go through review and the full deploy, which runs
`occ upgrade` for Nextcloud. For Nextcloud, `service-update.yml` also:

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
two hours after upstream publishes. Other auto-mergeable updates are proposed
in Renovate's nightly window (01:00–07:00), also outside the MR limits.

The `automerge:eligibility` job in MR pipelines runs checks 1–5 against the MR's
own checkout and prints the result. It has no credentials and decides nothing.

## Deploy and rollback

On a commit carrying the trailers, the full host deploys (`router:deploy`,
`zwave:deploy`, `satellite-2:deploy`, `macmini:deploy`) become manual jobs and
the `<host>:deploy-service` jobs named in `Automerge-Deploy` run instead, each
in its host's resource group (`deploy-router`, `deploy-zwave`,
`deploy-satellite-2`):

1. `automerge.py plan-deploy --host <job>` re-verifies the merge from `main`'s
   copy of the policy: the commit is a merge of an MR labelled `automerged` by
   an allowed author, merged by the auto-merge identity, the first-parent diff
   is image lines only in listed compose files, and every change passes rule 4.
   It then derives the hosts from the inventory and refuses if its own host is
   not one of them. If any check fails, nothing is deployed and the job fails;
   run the manual `<host>:deploy` if a full deploy is wanted.
2. `ansible/service-update.yml` (play host from the plan) fetches secrets,
   renders the host's compose files with the same task as the full deploy
   (`configure-homelab-services` `render_compose.yml`), then pulls and
   recreates **only** the changed services in that host's compose project
   (`dependencies: false`, no orphan removal).
3. Health gate (`ci/automerge/wait-healthy.sh`, per service, up to `timeout`,
   default 600s). The container (found by its compose project and service
   labels) must run the new digest, its Docker healthcheck must report
   `healthy` (or, without one, it must stay up for 60s without restarting),
   and the optional `probe` URL must return 2xx/3xx. Probes go to the internal
   reverse proxy (`lab_nginx_ip`) with the URL's hostname, so they don't
   depend on DNS. If the container exits or restarts three times, the gate
   fails straight away. Every long-running service in the listed compose files
   defines a healthcheck; `test_policy_scope.py` fails for one that does not
   and is not denylisted.
   For Nextcloud, see [Digest-only services and rollback](#digest-only-services-and-rollback).
4. On failure the playbook renders the **pre-merge** compose files (the merge
   commit's first parent), recreates the same services on their previous image,
   runs the health gate again, and fails the job. Then `automerge.py deploy-report`:
   - opens (or updates) an issue labelled `automerge-rollback` with the host,
     the failure and whether the rollback is healthy;
   - opens a revert MR (`automerge-revert/<iid>`) that restores the previous image
     lines of every changed file on `main`;
   - labels the original MR `automerge-rollback`;
   - mails root (via the router's msmtp) with the same details.

An update of `docker-compose-observability.yml` deploys on zwave and
satellite-2 in separate jobs. If one fails and the other succeeds, the revert
MR restores the file for both; the host that succeeded keeps the new image
until the revert is deployed (`<host>:deploy`).

Alerts:

- Rollback (health gate failed): mail from `deploy-report`, plus the issue.
- `plan-deploy` refused the merged commit (nothing deployed): mail from
  `automerge.py notify`.
- The job ended any other way without recording an outcome (timeout,
  cancellation, crash, host unreachable): mail from the job's `after_script`.
- A container still unhealthy afterwards also fires Grafana's
  `Container Health Check Failed` alert (email) after 10 minutes.

Rendering and recreating the previous image is idempotent, so you can retry the
job safely. Rollback restores the **image**, not data. A service whose upgrade
migrates its data on start may not run cleanly on the old image: restrict its
`updates:` (see [Restricted kinds](#restricted-kinds)).

While any `automerge-rollback` issue is open, auto-merge is paused for every MR.
`main` still references the new image until the revert MR is merged, so a full
deploy in the meantime would apply it again. Merge (or close) the revert
MR, then close the issue.

## Freezing

Any one of these stops auto-merge:

- **One MR:** add the `freeze` label.
- **Everything, temporarily:** set the CI/CD variable `AUTOMERGE_FREEZE=true`
  (project settings, or on the pipeline schedule).
- **Everything, in git:** commit an `ci/automerge-freeze` file (any content) to `main`.
- An open `automerge-rollback` issue (automatic).
- A per-service deploy still running (automatic, until it ends).

These apply to both classes. `AUTOMERGE_DRY_RUN=all` on the schedule also
stops both, but logs every decision. `fast_track.enabled: false` stops only
the fast track.

Removing the freeze needs no other action. The next sweep re-evaluates.

## Changing the policy

All in `ci/automerge-policy.yml` (reviewed, since `ci/` is code-owned):

- **Keep a service manual:** add it under `denylist.services` with a reason,
  and its image under `denylist.images` (and in the "Auto-merge denylist" and
  "Auto-merge class" rules of `renovate.json`).
- **Restrict its kinds:** set `updates:` under `services`, and exclude the image
  from the "Auto-merge class" rule in `renovate.json` (an "Auto-merge class,
  restricted kinds" rule gives it the allowed kinds). Use this for services
  whose version change runs a one-way migration.
- **Add a probe or timeout:** add the service under `services` with the compose
  **service name**, not the container name:

  ```yaml
  services:
    myservice:
      probe: https://myservice.lab.nkontur.com/health   # optional, must return 2xx/3xx
      probe_via: external                              # optional: probe through the external proxy
      timeout: 900                                     # optional, seconds
      updates: [digest, patch]                         # optional subset of [digest, patch, minor]
  ```

- **New compose file or host:** add the file to `docker_compose_file_names` in
  the inventory, list it under `compose_files`, map the host under
  `deploy_hosts`, add a `<host>:deploy-service` job (extends `.deploy_service`)
  matching `Automerge-Deploy:.*\[<host>\]`, add the file to the "Auto-merge
  class" rule in `renovate.json`, and make the auto-merge bot a second code
  owner of the file in `CODEOWNERS`. `test_policy_scope.py` and
  `test_codeowners.py` check each of these.
- **Retire a service:** put it under a compose profile. The sweep never merges
  an update of it; disable its image in `renovate.json` so Renovate stops
  proposing updates (`test_policy_scope.py` checks this).

## Root-authored MRs

GitLab never lets an author approve their own MR. For a code-owned path whose
only owner is `@root`, a root-authored MR has no eligible approver, GitLab treats
that rule as invalid and it does not block. The listed compose files also name
the automerge bot, so there the rule stays valid and a root-authored MR (Noah,
or an agent working as root) needs the bot's approval.

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
  author. It is listed as a second code owner of every file under
  `compose_files`, so its approval satisfies the required code-owner approval
  on `main`. Besides the sweep, it is used only by `scripts/approve-root-mr.sh`
  (see "Root-authored MRs") and by `report:cves` (labels and the dependency
  dashboard). Rotate it by creating a new project access token with the same
  name/role/scope and updating the variable; `CODEOWNERS` changes only if the
  bot user changes.
- Authors: `moltbot` and the Renovate bot (`RENOVATE_TOKEN`, see
  [RENOVATE.md](RENOVATE.md)). Renovate opens one MR per image and file, so a
  compose MR from it carries a single image change (possibly on several
  services that share that image).
- Pipeline schedules on `main` (owner `root`, timezone America/New_York):
  - `automerge sweep`: `*/15 * * * *`, variable `AUTOMERGE_TASK=sweep` (both
    classes live; `AUTOMERGE_DRY_RUN` removed 2026-11-04).
  - `automerge report`: `30 8 * * *`, variable `AUTOMERGE_TASK=report`.
- **Dry run**: while the sweep schedule has `AUTOMERGE_DRY_RUN=1`, the general
  class logs every decision (`ELIGIBLE` / `not eligible` with reasons, "would
  merge ...") and writes nothing. **The fast-track class stays live.**
  `AUTOMERGE_DRY_RUN=all` dry-runs both classes, and so does `sweep --dry-run`.
  To take the general class live, delete the variable from the schedule (Build >
  Pipeline schedules > `automerge sweep` > edit), or via the API:
  `glab api -X DELETE projects/4/pipeline_schedules/<id>/variables/AUTOMERGE_DRY_RUN`.
  To stop everything, set `AUTOMERGE_FREEZE=true` (or `AUTOMERGE_DRY_RUN=all`).
  To stop only the fast track, set `fast_track.enabled: false` in the policy
  file (reviewed MR).
- Dry run from a checkout of `main`, with a read-only token:
  `GITLAB_TOKEN=... python3 ci/automerge/automerge.py evaluate <iid> [<iid> ...]`
- Tests: `python3 -m unittest discover -s ci/automerge -v`. `test_fast_track.py`
  and `test_policy_scope.py` also check the real policy file, inventory,
  compose files, `renovate.json` and `.gitlab-ci.yml` against each other.
