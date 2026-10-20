# Auto-merge for image updates

Routine container image updates (patch/minor releases and digest refreshes) for
a short list of stateless services can merge and deploy without a manual review.
Everything else goes through the normal review and full deploy.

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
4. Every changed service is listed under `services` in the allowlist.
5. No CODEOWNERS-protected path is touched (the compose file is the one exception,
   and only under rule 3).
6. Nothing is frozen (see below).
7. Fewer than `max_per_24h` (default 3) MRs carry the `automerged` label with a
   merge time in the last 24 hours.
8. The router's ZFS pools all report `ONLINE` and no resilver is running. Any error
   while checking counts as a failure. A pool listed under `storage_known_state` in
   the allowlist may instead be `DEGRADED` while a resilver is in progress, but only
   if it is no worse than recorded there: no other vdev `FAULTED`, `UNAVAIL`,
   `OFFLINE` or `REMOVED`, no `READ`/`WRITE`/`CKSUM` counter above its recorded
   value (0 if not listed), no data errors, and I/O not suspended. A listed pool
   that is `DEGRADED` without a resilver running still blocks.

Before merging, the sweep also requires: the MR pipeline for the current head
commit succeeded, the MR is not a draft and has no conflicts. MRs labelled
`security` (set by the daily running-image CVE scan, or by Renovate for advisories)
are considered first. It merges at most one MR per run, through the API with the `sha` guard, so a push after evaluation
blocks the merge. The merge is done by a dedicated CI identity (`AUTOMERGE_TOKEN`),
never by the MR author. The merge commit message ends with the trailer
`Automerge-Policy: image-update`, which the `main` pipeline uses to select the
per-service deploy.

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
4. On failure the playbook renders the **pre-merge** compose file (the merge
   commit's first parent), recreates the same services on their previous image,
   runs the health gate again, and fails the job. Then `automerge.py deploy-report`:
   - opens (or updates) an issue labelled `automerge-rollback` with the failure and
     whether the rollback is healthy;
   - opens a revert MR (`automerge-revert/<iid>`) that restores the previous image
     lines on `main`;
   - labels the original MR `automerge-rollback`.

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

Removing the freeze needs no other action. The next sweep re-evaluates.

## Adding a service

Add it under `services` in `ci/automerge-allowlist.yml` (reviewed, since `ci/` is
code-owned). Use the compose **service name**, not the container name:

```yaml
services:
  myservice:
    probe: https://myservice.lab.nkontur.com/health   # optional, must return 2xx/3xx
    timeout: 900                                     # optional, seconds
```

Only add services that are safe to restart on their own and that can go back to the
previous image without manual steps. That rules out databases, anything that
migrates data one way on upgrade, and anything the deploy itself depends on.
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
- **Dry run**: while the sweep schedule has `AUTOMERGE_DRY_RUN=1` it logs every
  decision (`ELIGIBLE` / `not eligible` with reasons, "would merge ...") and writes
  nothing. To go live, delete that variable from the schedule (Build > Pipeline
  schedules > `automerge sweep` > edit), or via the API:
  `glab api -X DELETE projects/4/pipeline_schedules/<id>/variables/AUTOMERGE_DRY_RUN`.
  Set it again (or `AUTOMERGE_FREEZE=true`) to stop.
- Dry run from a checkout of `main`, with a read-only token:
  `GITLAB_TOKEN=... python3 ci/automerge/automerge.py evaluate <iid> [<iid> ...]`
- Tests: `python3 -m unittest discover -s ci/automerge -v`
