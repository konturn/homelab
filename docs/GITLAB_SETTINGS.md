# GitLab project settings in code

Some settings the pipeline relies on cannot be set in `.gitlab-ci.yml`, and the
GitLab Terraform provider (19.x) has no resource for resource groups. They are
enforced by [`scripts/gitlab-settings.sh`](../scripts/gitlab-settings.sh), run by
the `gitlab:settings` CI job.

| Setting | Value | Why |
|---|---|---|
| Resource groups `deploy-router`, `deploy-zwave`, `deploy-satellite-2`, `deploy-macmini`: `process_mode` | `newest_ready_first` | One deploy per host at a time, and the newest commit's deploy goes next instead of the oldest queued one. |
| Project `ci_forward_deployment_enabled` ("Prevent outdated deployment jobs") | `true` | With the deploy jobs' `environment:`, an older deploy still waiting for its resource group is skipped once a newer one has run. |
| Instance plan limit `ci_pipeline_schedules` (plan `default`) | `20` | GitLab allows 10 pipeline schedules per project by default; this project has 11 (deploy, reports, drift checks, renovate, automerge, this job). Raised by hand on 2026-10-20 (Admin > plan limits, the same as `PUT /application/plan_limits`), now enforced here. |

`router:deploy` and `router:deploy-service` share `deploy-router`. Resource
groups for the other jobs (`netgear-switches`, `aruba-iap`, `automerge`,
`renovate`, `report-trivy`) stay `unordered` on purpose.

## How it runs

- `gitlab:settings`: main only. A daily pipeline schedule with
  `SCHEDULED_TASK=gitlab-settings` (only this job runs in it), and on pushes to
  main that change the script or `.gitlab-ci.yml`. It can also be started from
  Build > Pipelines > Run pipeline on `main` with `SCHEDULED_TASK=gitlab-settings`.
- The script GETs each setting and PUTs only what differs. Output, one line each:
  `ok ...`, `FIXED ... <old> -> <new>`, `skip ...` (a resource group GitLab has
  not created yet: it appears with the first job that uses it), or `ERROR ...`.
  Any error fails the job. A `FIXED` line means someone changed the setting by
  hand.
- Token: `GITLAB_SETTINGS_TOKEN`, set in the job from `APPROLE_ROTATION_PAT`
  (protected, masked; the "Approle rotator" project access token, Maintainer,
  `api`). Resource groups would work with Developer (`AUTOMERGE_TOKEN`), but
  reading or changing `ci_forward_deployment_enabled` needs Maintainer; below
  that the API returns `null` for it, which the script reports as an error.

### The plan limit: no admin token

Plan limits are instance settings: the API (`GET`/`PUT /application/plan_limits`)
needs an administrator, and a token for that (a PAT of an admin user with
`api` + `admin_mode`) could change any instance setting. No such token is
created. The script reads and sets the limit inside the router's `gitlab`
container instead:

- read: `gitlab-psql` (`plan_limits` joined to `plans`, plan `default`), under a second;
- fix: `gitlab-rails runner 'Plan.default.actual_limits.update!(ci_pipeline_schedules: 20)'`
  (about 2 minutes to boot Rails; only on drift), then read again.

On the router it runs `docker exec gitlab ...`. In CI it SSHes to
`root@10.4.0.1` with `ROUTER_PRIVATE_KEY_BASE64`, the protected deploy key
that `router:deploy` and the drift jobs already hold, so the job gets no
access that main pipelines did not already have and no new credential exists.
The job writes the key to a 0600 file and removes it in `after_script`.

## Running it by hand

On the router, from the repo checkout (`glab` logged in as root; the script
reads `APPROLE_ROTATION_PAT` through `glab` and never prints it):

```bash
scripts/gitlab-settings.sh --check   # report only; exit 3 on drift
scripts/gitlab-settings.sh           # fix drift
```

Off the router, set `ROUTER_SSH_KEY` to a key file for `root@10.4.0.1`, or the
plan limit line reports `ERROR`.

## Adding a setting

Add the resource group key to `RESOURCE_GROUPS`, or another GET/compare/PUT
block for a project setting, in the script and in the table above.
