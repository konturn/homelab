# GitLab project settings in code

Some settings the pipeline relies on cannot be set in `.gitlab-ci.yml`, and the
GitLab Terraform provider (19.x) has no resource for resource groups. They are
enforced by [`scripts/gitlab-settings.sh`](../scripts/gitlab-settings.sh), run by
the `gitlab:settings` CI job.

| Setting | Value | Why |
|---|---|---|
| Resource groups `deploy-router`, `deploy-zwave`, `deploy-satellite-2`, `deploy-macmini`: `process_mode` | `newest_ready_first` | One deploy per host at a time, and the newest commit's deploy goes next instead of the oldest queued one. |
| Project `ci_forward_deployment_enabled` ("Prevent outdated deployment jobs") | `true` | With the deploy jobs' `environment:`, an older deploy still waiting for its resource group is skipped once a newer one has run. |

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

## Running it by hand

On the router, from the repo checkout (`glab` logged in as root; the script
reads `APPROLE_ROTATION_PAT` through `glab` and never prints it):

```bash
scripts/gitlab-settings.sh --check   # report only; exit 3 on drift
scripts/gitlab-settings.sh           # fix drift
```

## Adding a setting

Add the resource group key to `RESOURCE_GROUPS`, or another GET/compare/PUT
block for a project setting, in the script and in the table above.
