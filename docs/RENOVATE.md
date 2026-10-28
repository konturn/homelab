# Renovate

Dependency updates are detected and proposed by a self-hosted
[Renovate](https://docs.renovatebot.com/) that runs as a scheduled CI job on
`main`. Configuration: [`renovate.json`](../renovate.json).

| Piece | Where |
|---|---|
| Job | `renovate` in `.gitlab-ci.yml`, image pinned by digest |
| Schedule | pipeline schedule on `main`, `SCHEDULED_TASK=renovate`, `15 * * * *` (hourly) America/New_York. Ordinary updates are still only *created* 01:00–07:00 (`schedule` in `renovate.json`); the other runs pick up fast-track and security updates and keep existing MRs current |
| Identity | project access token `renovate-bot` (Developer, `api`, 1-year expiry) in the masked + protected CI variable `RENOVATE_TOKEN` |
| Dashboard | GitLab issue "Renovate dependency dashboard" (lists pending, rate-limited and approval-gated updates) |
| Config check | `renovate:validate` in MR pipelines that touch `renovate.json` |

## What it watches

- `docker/docker-compose.yml`: a Jinja template, so the docker-compose manager
  cannot parse it. A regex manager reads its `image: name:tag@sha256:digest` lines.
  Updates keep the digest pin.
- `docker/docker-compose-*.yml` (docker-compose manager), Dockerfiles, `.gitlab-ci.yml`
  images, `requirements.txt`, `ansible/requirements.yml`, `terraform/vault`, npm.
- `npm install -g <pkg>@<x.y.z>` lines in Dockerfiles (e.g. the Claude Code CLI
  in `docker/openclaw/Dockerfile`).
- Annotated pins anywhere under `ansible/`, `scripts/` or in Dockerfiles: put
  `# renovate: datasource=<datasource> depName=<name>` (optionally
  `versioning=<v>` and `extractVersion=<regex>`) on the line directly above a
  `key: value` or `KEY="value"` line. Used for gitleaks and virtio-win.

Held back on purpose (see `packageRules`): promtail `>= 3.6`, influxdb 3,
Headway/Pelias images (they must move together with the separately built map
data), locally built images in the GitLab registry, and `mariadb` (Nextcloud's
database), which only gets patch and digest updates within the 12.3 LTS series.
`ollama/ollama` updates wait for dashboard approval: each one must be checked
for the CUDA 12 runner the Pascal GPU needs ([ollama.md](ollama.md#testing-an-update)).

## Policy

- One MR per package (no groups). The router compose file gets its own branches
  (`renovate/router-*`, title suffix `(router)`), so an MR touching
  `docker/docker-compose.yml` changes exactly one image and nothing else. That is
  what the auto-merge policy ([AUTOMERGE.md](AUTOMERGE.md)) evaluates; the Renovate
  bot is an allowed author there and the merge is done by a different identity.
- Major updates wait for approval on the dependency dashboard (tick the box).
  Image majors are researched and opened by moltbot's `image-update-check` cron
  instead, one MR per service, never auto-merged.
- Digest refreshes are batched to Monday mornings, except for fast-track images
  (below).
- **Fast track** (two rules near the end of `packageRules`). For the images of
  services reachable from outside the LAN, in the router compose file only:
  digest refreshes, `pinDigest` and patch releases are created at any hour
  (`schedule: at any time`) and outside the MR limits (`prConcurrentLimit`,
  `branchConcurrentLimit` and `prHourlyLimit` 0 for those branches), with
  priority 10 and the label `fast-track`. Patch releases get their own branch
  even when a minor is also available (`separateMinorPatch`). Renovate itself
  never merges (`automerge: false`): the auto-merge sweep does, under the
  fast-track class ([AUTOMERGE.md](AUTOMERGE.md#fast-track)). That policy, not
  this rule, decides what merges without review. The `fast-track` label only
  means "proposed promptly": a Nextcloud, Vaultwarden or WordPress *patch*
  release gets it too, but still needs a human, because those are digest-only.
  The image list must match `fast_track.images` in `ci/automerge-allowlist.yml`;
  `ci/automerge/test_fast_track.py` checks this.
- Limits: 8 open Renovate MRs at a time, 4 new per hour, MRs created between
  01:00 and 07:00. Fast-track MRs are exempt from all three (they still count
  towards the 8, so a burst of them can delay ordinary MRs). Security fixes (OSV/GitLab advisories) ignore the schedule and
  get the `security` label; so do MRs for images the daily running-image CVE scan
  flags.
- Labels: `renovate` on everything, `security` when it applies.

## Operating

- Validate: `renovate:validate` runs `renovate-config-validator` in MR pipelines.
  Locally, from the repo root:
  `docker run --rm -v "$PWD/renovate.json:/repo/renovate.json:ro" -w /repo --entrypoint renovate-config-validator renovate/renovate:<tag>`
  (run it from the directory, not with a path argument: given a path, the
  validator checks the file as *global* config).
- Run now: Build > Pipeline schedules > `renovate` > play, or
  `glab api -X POST projects/4/pipeline_schedules/<id>/play`.
- Pause: deactivate the schedule. Renovate also stops touching an MR once someone
  else pushes to its branch.
- Rotate the token: create a new project access token with the same name, role
  and scope, update `RENOVATE_TOKEN`, then revoke the old one. If the bot user
  changes, update `authors` in `ci/automerge-allowlist.yml`.
- Local dry run (no token, no writes): from a checkout,
  `docker run --rm -v "$PWD:/repo" -w /repo renovate/renovate:<tag> renovate --platform=local`.
