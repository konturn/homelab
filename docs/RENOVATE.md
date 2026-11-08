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
  images, `requirements.txt`, `ansible/requirements.yml`, `terraform/vault`, npm,
  and the runners' default images in `base/gitlab-runner/config.toml` (regex manager).
  `docker-compose-zigbee.yml` and `docker-compose-satellite.yml` are ignored:
  no host renders them (`docker_compose_file_names` in the inventory), so an
  update there would deploy nothing.
- `npm install -g <pkg>@<x.y.z>` lines in Dockerfiles (e.g. the Claude Code CLI
  in `docker/openclaw/Dockerfile`).
- Annotated pins anywhere under `ansible/`, `scripts/` or in Dockerfiles: put
  `# renovate: datasource=<datasource> depName=<name>` (optionally
  `versioning=<v>` and `extractVersion=<regex>`) on the line directly above a
  `key: value` or `KEY="value"` line. Used for gitleaks and virtio-win.

Held back on purpose (see `packageRules`): the images used only by services
under the retired `wordpress` compose profile (`mysql`, `wordpress`), promtail `>= 3.6`, influxdb 3,
locally built images in the GitLab registry, and `mariadb` (Nextcloud's
database), which only gets patch and digest updates within the 12.3 LTS series.
`ollama/ollama` updates wait for dashboard approval: each one must be checked
for the CUDA 12 runner the Pascal GPU needs ([ollama.md](ollama.md#testing-an-update)).

## Policy

- One MR per package (no groups). The router compose file gets its own branches
  (`renovate/router-*`, title suffix `(router)`), so an MR touching
  `docker/docker-compose.yml` changes exactly one image and nothing else. That is
  what the auto-merge policy ([AUTOMERGE.md](AUTOMERGE.md)) evaluates; the Renovate
  bot is an allowed author there and the merge is done by a different identity.
- Major updates wait for approval on the dependency dashboard (tick the box)
  and carry the label `major`; the auto-merge sweep never merges an MR with
  that label. moltbot's `image-update-check` cron researches majors (a comment
  on Renovate's MR or the tracking issue) and never opens image MRs itself.
- **Auto-merge class** (rules "Auto-merge class" after the weekly digest rule).
  Patch, minor, digest and `pinDigest` updates in the deployed compose files
  (`compose_files` in `ci/automerge-policy.yml`), except the denylisted images
  and the images with restricted kinds, are the ones the sweep merges on its
  own ([AUTOMERGE.md](AUTOMERGE.md)). They are proposed every night in the
  01:00–07:00 window (digest refreshes too) and outside the MR limits
  (`prConcurrentLimit` and `branchConcurrentLimit` 0, at most 4 new per hour),
  so updates waiting for a human cannot hold them back. The two digest-only
  images (Nextcloud, Vaultwarden) are excluded here; their digest refreshes come
  through the fast track.
- **CI image class** (rule "CI image class"): patch, minor, digest and
  `pinDigest` updates of job images in `.gitlab-ci.yml`, on the same nightly
  schedule and limits as the auto-merge class. The sweep merges them
  ([AUTOMERGE.md](AUTOMERGE.md#ci-images)); `willhallonline/ansible` is on the
  CI image denylist and labelled `automerge-manual`.
- **Denylist** (rule "Auto-merge denylist"): GitLab, Vault, the Docker socket
  proxy and the GitLab runner image get the label `automerge-manual` and a
  note in the MR body; the sweep never merges them.
- **Dependency class** (rules "Dependency class ..."): patch, minor, digest
  and `pinDigest` updates of Dockerfile base images and pins, the OpenClaw
  tool npm dependencies and annotated ansible pins, on the same nightly
  schedule and limits as the auto-merge class. The sweep merges them
  ([AUTOMERGE.md](AUTOMERGE.md#dependency-updates)). `.gitlab-ci.yml` updates get
  `ci-` branches, so they never share an MR with a Dockerfile update of the
  same image; the security-critical Dockerfiles (JIT approval
  service, event-router) get `critical-` branches with patches apart from
  minors, and their minors are labelled `automerge-manual`, as are updates of
  the OpenClaw base image.
- CI job images are pinned by digest (`gitlabci.pinDigests`): an image
  referenced by tag only gets a "pin" MR first.
- Other digest refreshes (the denylisted images) are batched to Monday
  mornings.
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
  means "proposed promptly": a Nextcloud or Vaultwarden *patch*
  release gets it too, but still needs a human, because those are digest-only.
  The image list must match `fast_track.images` in `ci/automerge-policy.yml`;
  `ci/automerge/test_fast_track.py` checks this.
- Limits: 8 open Renovate MRs at a time, 4 new per hour, MRs created between
  01:00 and 07:00. Fast-track MRs are exempt from all three, auto-merge class
  MRs from the first and the branch limit. Both still count towards the 8, so
  a backlog of them can delay the MRs that need a human (majors, denylisted images)
  until the sweep has merged it. Security fixes (OSV/GitLab
  advisories) ignore the schedule and get the `security` label.
- Running-image CVE scan (`report:cves`, daily): labels open MRs for affected
  images `security` (the sweep considers those first), and ticks the
  "Awaiting Schedule" / "Rate-Limited" boxes of their held-back updates on the
  dependency dashboard, so Renovate creates them on its next hourly run
  instead of waiting for the schedule (`ci/reports/trivy_label.py`).
- Labels: `renovate` on everything, `security` when it applies, `major` on
  major updates, `automerge-manual` on denylisted images (the sweep adds it to
  any other update it leaves to a human).

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
  changes, update `authors` in `ci/automerge-policy.yml`.
- Local dry run (no token, no writes): from a checkout,
  `docker run --rm -v "$PWD:/repo" -w /repo renovate/renovate:<tag> renovate --platform=local`.
