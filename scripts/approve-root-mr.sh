#!/usr/bin/env bash
# approve-root-mr.sh: approve a root-authored MR as the automerge-bot identity.
#
# Root cannot approve its own MRs, and docker/docker-compose.yml lists the
# automerge-bot as its only other code owner, so a root-authored MR touching it
# needs the bot's approval. This script gives it, with guard rails:
#
#   - the MR author must be root (never moltbot, Renovate or anyone else:
#     their MRs go through human review or ci/automerge/policy.py);
#   - target main, open, not a draft;
#   - the head pipeline for the current head commit succeeded;
#   - the approval is pinned to that head commit (sha guard). Approvals reset
#     on push, so a later push needs a fresh run.
#
# Run on the router from the repo checkout (glab must be logged in as root).
# The token is read from the AUTOMERGE_TOKEN CI variable and never printed.
# See docs/AUTOMERGE.md ("Root-authored MRs").
#
# Usage: scripts/approve-root-mr.sh <mr-iid>
set -euo pipefail

PROJECT_ID=4
ROOT_USER_ID=1
API="${GITLAB_API:-https://gitlab.lab.nkontur.com/api/v4}"
IID="${1:?usage: $0 <mr-iid>}"

fail() { echo "refusing: $*" >&2; exit 1; }

mr=$(glab api "projects/${PROJECT_ID}/merge_requests/${IID}")
author_id=$(jq -r .author.id <<<"$mr")
state=$(jq -r .state <<<"$mr")
draft=$(jq -r .draft <<<"$mr")
target=$(jq -r .target_branch <<<"$mr")
sha=$(jq -r .sha <<<"$mr")
pipe_sha=$(jq -r '.head_pipeline.sha // ""' <<<"$mr")
pipe_status=$(jq -r '.head_pipeline.status // ""' <<<"$mr")

[ "$author_id" = "$ROOT_USER_ID" ] || fail "author is $(jq -r .author.username <<<"$mr"), not root"
[ "$state" = opened ] || fail "MR is $state"
[ "$draft" = false ] || fail "MR is a draft"
[ "$target" = main ] || fail "target is $target, not main"
[ "$pipe_sha" = "$sha" ] || fail "head pipeline is for ${pipe_sha:-nothing}, not head commit $sha"
[ "$pipe_status" = success ] || fail "head pipeline status is ${pipe_status:-none}"

token=$(glab api "projects/${PROJECT_ID}/variables/AUTOMERGE_TOKEN" | jq -r .value)
{ [ -n "$token" ] && [ "$token" != null ]; } || fail "could not read AUTOMERGE_TOKEN"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  --header @<(printf 'PRIVATE-TOKEN: %s\n' "$token") \
  --data-urlencode "sha=${sha}" \
  "${API}/projects/${PROJECT_ID}/merge_requests/${IID}/approve")
unset token
case "$code" in
  200|201) echo "approved !${IID} at ${sha:0:8} as automerge-bot" ;;
  401) fail "approve returned 401 (already approved by the bot, or token invalid)" ;;
  409) fail "approve returned 409 (head changed; re-run after the new pipeline passes)" ;;
  *) fail "approve returned HTTP $code" ;;
esac

glab api "projects/${PROJECT_ID}/merge_requests/${IID}" | jq -r '"detailed_merge_status: \(.detailed_merge_status)"'
