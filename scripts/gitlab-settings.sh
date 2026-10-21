#!/usr/bin/env bash
# gitlab-settings.sh: enforce GitLab project settings that .gitlab-ci.yml
# cannot express. Idempotent: reads each setting and writes it only if it
# differs. Run by the gitlab:settings CI job (main, scheduled daily and on
# changes to this file); see docs/GITLAB_SETTINGS.md.
#
#   - process_mode=newest_ready_first on the deploy resource groups, so a
#     queued older deploy never runs after a newer one (together with
#     ci_forward_deployment_enabled, older pending deploys are skipped).
#   - ci_forward_deployment_enabled=true ("Prevent outdated deployment jobs").
#
# Token: GITLAB_SETTINGS_TOKEN (CI: APPROLE_ROTATION_PAT, a Maintainer project
# access token; the project setting needs Maintainer). When unset and glab is
# logged in (router checkout), the CI variable is read through glab. The token
# is never printed.
#
# Usage: scripts/gitlab-settings.sh [--check]
#   --check   report drift only; exit 3 if anything differs
set -euo pipefail

API="${CI_API_V4_URL:-https://gitlab.lab.nkontur.com/api/v4}"
PROJECT_ID="${CI_PROJECT_ID:-4}"
WANT_MODE=newest_ready_first
# The resource_group keys of router:deploy / router:deploy-service,
# zwave:deploy, satellite-2:deploy and macmini:deploy in .gitlab-ci.yml.
RESOURCE_GROUPS=(deploy-router deploy-zwave deploy-satellite-2 deploy-macmini)

check=false
case "${1:-}" in
  --check) check=true ;;
  "") ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

token="${GITLAB_SETTINGS_TOKEN:-}"
if [ -z "$token" ] && command -v glab >/dev/null 2>&1; then
  token=$(glab api "projects/${PROJECT_ID}/variables/APPROLE_ROTATION_PAT" | jq -r .value) || token=
fi
if [ -z "$token" ] || [ "$token" = null ]; then
  echo "ERROR: no token (set GITLAB_SETTINGS_TOKEN)" >&2
  exit 1
fi

body=$(mktemp)
trap 'rm -f "$body"' EXIT

# api METHOD PATH [curl args...]: response body in $body, HTTP status on stdout.
# The token goes through a header file on stdin, not the command line.
api() {
  local method=$1 path=$2
  shift 2
  printf 'PRIVATE-TOKEN: %s\n' "$token" |
    curl -sS -o "$body" -w '%{http_code}' -X "$method" -H @- \
      "${API}/projects/${PROJECT_ID}${path}" "$@"
}

drift=0
errors=0

for rg in "${RESOURCE_GROUPS[@]}"; do
  code=$(api GET "/resource_groups/${rg}")
  if [ "$code" = 404 ]; then
    # GitLab creates a resource group the first time a job uses it.
    echo "skip   resource_group ${rg}: does not exist yet"
    continue
  elif [ "$code" != 200 ]; then
    echo "ERROR  resource_group ${rg}: GET returned HTTP ${code}"
    errors=$((errors + 1)); continue
  fi
  mode=$(jq -r .process_mode "$body")
  if [ "$mode" = "$WANT_MODE" ]; then
    echo "ok     resource_group ${rg}: process_mode=${mode}"
    continue
  fi
  drift=$((drift + 1))
  if $check; then
    echo "DRIFT  resource_group ${rg}: process_mode=${mode} (want ${WANT_MODE})"
    continue
  fi
  code=$(api PUT "/resource_groups/${rg}" --data "process_mode=${WANT_MODE}")
  if [ "$code" = 200 ] && [ "$(jq -r .process_mode "$body")" = "$WANT_MODE" ]; then
    echo "FIXED  resource_group ${rg}: process_mode ${mode} -> ${WANT_MODE}"
  else
    echo "ERROR  resource_group ${rg}: PUT returned HTTP ${code}"
    errors=$((errors + 1))
  fi
done

code=$(api GET "")
fwd=$(jq -r .ci_forward_deployment_enabled "$body" 2>/dev/null || echo null)
if [ "$code" != 200 ] || [ "$fwd" = null ]; then
  # Below Maintainer the API omits CI settings (null) instead of failing.
  echo "ERROR  project ci_forward_deployment_enabled: cannot read it (HTTP ${code}; token needs Maintainer)"
  errors=$((errors + 1))
elif [ "$fwd" = true ]; then
  echo "ok     project ci_forward_deployment_enabled=true"
else
  drift=$((drift + 1))
  if $check; then
    echo "DRIFT  project ci_forward_deployment_enabled=${fwd} (want true)"
  else
    code=$(api PUT "" --data ci_forward_deployment_enabled=true)
    if [ "$code" = 200 ] && [ "$(jq -r .ci_forward_deployment_enabled "$body")" = true ]; then
      echo "FIXED  project ci_forward_deployment_enabled ${fwd} -> true"
    else
      echo "ERROR  project ci_forward_deployment_enabled: PUT returned HTTP ${code}"
      errors=$((errors + 1))
    fi
  fi
fi

if $check; then
  echo "${drift} setting(s) differ, ${errors} error(s)"
else
  echo "${drift} setting(s) fixed, ${errors} error(s)"
fi
[ "$errors" -eq 0 ] || exit 1
if $check && [ "$drift" -gt 0 ]; then exit 3; fi
exit 0
