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
#   - reset_approvals_on_push=true ("Remove all approvals when commits are
#     added to the source branch"): scripts/approve-root-mr.sh and the
#     automerge sweep approve one reviewed head commit and rely on it.
#   - instance plan limit ci_pipeline_schedules=20 (default plan), so the
#     project can hold more than GitLab's default of 10 pipeline schedules.
#   - the event-router webhooks (pipeline + job events to
#     http://event-router:8080/gitlab, secret token from Vault) on projects
#     4 and 14, and `event-router` in the instance's outbound local-request
#     allowlist, which GitLab needs to deliver to the container.
#
# Token: GITLAB_SETTINGS_TOKEN (CI: APPROLE_ROTATION_PAT, a Maintainer project
# access token; the project setting needs Maintainer). When unset and glab is
# logged in (router checkout), the CI variable is read through glab. The token
# is never printed.
#
# The plan limit is instance-wide and needs GitLab admin, which the project
# token does not have. It is read and set inside the router's gitlab
# container instead (gitlab-psql to read, gitlab-rails runner to fix): with
# docker when run on the router, otherwise over SSH as root@10.4.0.1 with the
# key in ROUTER_SSH_KEY (CI: ROUTER_PRIVATE_KEY_BASE64, the key the deploy
# jobs already use). No admin API token exists for this.
#
# The webhooks are checked the same way, in one `gitlab-rails runner` (about
# 2 minutes): the project token cannot see project 14's hooks, and GitLab
# never returns a hook's URL secret or token over the API. The webhook token
# is read from Vault (homelab/docker/event-router, gitlab_webhook_token; CI:
# JWT login with VAULT_ID_TOKEN, role ci-deploy; router: VAULT_TOKEN, or the
# CI variable through glab) and handed to the runner on stdin, never on a
# command line.
#
# Usage: scripts/gitlab-settings.sh [--check]
#   --check   report drift only; exit 3 if anything differs
set -euo pipefail

API="${CI_API_V4_URL:-https://gitlab.lab.nkontur.com/api/v4}"
PROJECT_ID="${CI_PROJECT_ID:-4}"
WANT_MODE=newest_ready_first
# The resource_group keys of the <host>:deploy / <host>:deploy-service jobs,
# for router, zwave and satellite-2, and macmini:deploy, in .gitlab-ci.yml.
RESOURCE_GROUPS=(deploy-router deploy-zwave deploy-satellite-2 deploy-macmini)
# Instance limit on pipeline schedules per project (GitLab default: 10).
# The project has 11 schedules (docs/GITLAB_SETTINGS.md).
WANT_PIPELINE_SCHEDULES=20
ROUTER_HOST="${ROUTER_HOST:-10.4.0.1}"
# Event-router webhooks (docker/event-router/README.md, "GitLab events").
HOOK_PROJECTS=(4 14)
HOOK_URL=http://event-router:8080/gitlab
HOOK_ALLOWLIST_ENTRY=event-router
VAULT_ADDR="${VAULT_ADDR:-https://vault.lab.nkontur.com:8200}"
HOOK_TOKEN_PATH=homelab/data/docker/event-router
KNOWN_HOSTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/ansible/known_hosts"

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

code=$(api GET "/approvals")
reset=$(jq -r .reset_approvals_on_push "$body" 2>/dev/null || echo null)
if [ "$code" != 200 ] || [ "$reset" = null ]; then
  echo "ERROR  project reset_approvals_on_push: cannot read it (HTTP ${code}; token needs Maintainer)"
  errors=$((errors + 1))
elif [ "$reset" = true ]; then
  echo "ok     project reset_approvals_on_push=true"
else
  drift=$((drift + 1))
  if $check; then
    echo "DRIFT  project reset_approvals_on_push=${reset} (want true)"
  else
    code=$(api POST "/approvals" --data reset_approvals_on_push=true)
    if { [ "$code" = 201 ] || [ "$code" = 200 ]; } && [ "$(jq -r .reset_approvals_on_push "$body")" = true ]; then
      echo "FIXED  project reset_approvals_on_push ${reset} -> true"
    else
      echo "ERROR  project reset_approvals_on_push: POST returned HTTP ${code}"
      errors=$((errors + 1))
    fi
  fi
fi

# gitlab_exec [-i] CMD...: run CMD in the router's gitlab container (-i: pass
# stdin through).
gitlab_exec() {
  local i=
  if [ "${1:-}" = -i ]; then i=-i; shift; fi
  if command -v docker >/dev/null 2>&1 && docker inspect gitlab >/dev/null 2>&1; then
    docker exec $i gitlab "$@"
  elif [ -n "${ROUTER_SSH_KEY:-}" ] && [ -s "$ROUTER_SSH_KEY" ]; then
    # Same SSH options as the other CI jobs that reach the router: host key
    # pinned in ansible/known_hosts and checked strictly.
    ssh -i "$ROUTER_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=15 \
      -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KNOWN_HOSTS" -o LogLevel=ERROR \
      "root@${ROUTER_HOST}" "docker exec $i gitlab $(printf '%q ' "$@")"
  else
    return 127
  fi
}

LIMIT_SQL="SELECT l.ci_pipeline_schedules FROM plan_limits l JOIN plans p ON p.id = l.plan_id WHERE p.name = 'default'"
read_schedule_limit() {
  gitlab_exec gitlab-psql -d gitlabhq_production -tAc "$LIMIT_SQL" | tr -d '[:space:]'
}

if ! limit=$(read_schedule_limit); then
  echo "ERROR  plan_limits default ci_pipeline_schedules: cannot read it (needs the gitlab container: run on the router, or set ROUTER_SSH_KEY)"
  errors=$((errors + 1))
elif [ "$limit" = "$WANT_PIPELINE_SCHEDULES" ]; then
  echo "ok     plan_limits default ci_pipeline_schedules=${limit}"
else
  drift=$((drift + 1))
  if $check; then
    echo "DRIFT  plan_limits default ci_pipeline_schedules=${limit:-unset} (want ${WANT_PIPELINE_SCHEDULES})"
  else
    # Through the model, as PUT /application/plan_limits does (~2 min to boot rails).
    gitlab_exec gitlab-rails runner \
      "Plan.default.actual_limits.update!(ci_pipeline_schedules: ${WANT_PIPELINE_SCHEDULES})" >/dev/null || true
    new=$(read_schedule_limit || true)
    if [ "$new" = "$WANT_PIPELINE_SCHEDULES" ]; then
      echo "FIXED  plan_limits default ci_pipeline_schedules ${limit:-unset} -> ${new}"
    else
      echo "ERROR  plan_limits default ci_pipeline_schedules: still ${new:-unset} after update"
      errors=$((errors + 1))
    fi
  fi
fi

# --- event-router webhooks and the outbound local-request allowlist ---------

# vault_get FIELD: one field of HOOK_TOKEN_PATH on stdout (empty on failure).
vault_get() {
  local vt="${VAULT_TOKEN:-}" k=()
  [ "${VAULT_SKIP_VERIFY:-}" = true ] && k=(-k)
  if [ -z "$vt" ] && [ -n "${VAULT_ID_TOKEN:-}" ]; then
    vt=$(jq -nc --arg r "${VAULT_JWT_ROLE:-ci-deploy}" --arg j "$VAULT_ID_TOKEN" '{role: $r, jwt: $j}' |
      curl -sS "${k[@]}" --max-time 15 -X POST --data @- "${VAULT_ADDR}/v1/auth/jwt/login" |
      jq -r '.auth.client_token // empty') || vt=
  fi
  if [ -z "$vt" ] && command -v glab >/dev/null 2>&1; then
    vt=$(glab variable get VAULT_TOKEN 2>/dev/null) || vt=
  fi
  [ -n "$vt" ] || return 1
  printf 'X-Vault-Token: %s\n' "$vt" |
    curl -sS "${k[@]}" --max-time 15 -H @- "${VAULT_ADDR}/v1/${HOOK_TOKEN_PATH}" |
    jq -r --arg f "$1" '.data.data[$f] // empty'
}

hook_token=$(vault_get gitlab_webhook_token || true)
if [ -z "$hook_token" ]; then
  echo "ERROR  webhooks: cannot read gitlab_webhook_token from Vault (${HOOK_TOKEN_PATH}); tokens not checked"
  errors=$((errors + 1))
fi

# Ruby for gitlab-rails runner. Prints one ok/DRIFT/FIXED/ERROR line per item.
# Every event the hook does not need is kept off; attributes this GitLab
# version does not have are ignored.
HOOK_RUBY=$(cat <<RUBY
check = ${check}
want_url = "${HOOK_URL}"
entry = "${HOOK_ALLOWLIST_ENTRY}"
tok = \$hook_token.to_s
on = %i[pipeline_events job_events]
off = %i[push_events tag_push_events merge_requests_events repository_update_events issues_events
         confidential_issues_events note_events confidential_note_events wiki_page_events
         deployment_events feature_flag_events releases_events milestone_events emoji_events
         resource_access_token_events resource_deploy_token_events vulnerability_events member_events]
want = { enable_ssl_verification: false }
on.each { |e| want[e] = true }
off.each { |e| want[e] = false }
say = ->(s) { puts s; \$stdout.flush }
begin
  s = ApplicationSetting.current_without_cache
  list = Array(s.outbound_local_requests_whitelist)
  if list.include?(entry)
    say.("ok     outbound local-request allowlist contains #{entry}")
  elsif check
    say.("DRIFT  outbound local-request allowlist: #{entry} missing")
  else
    s.update!(outbound_local_requests_whitelist: list + [entry])
    say.("FIXED  outbound local-request allowlist: added #{entry}")
  end
rescue => e
  say.("ERROR  outbound local-request allowlist: #{e.class}: #{e.message[0, 200]}")
end
%w[${HOOK_PROJECTS[*]}].map(&:to_i).each do |pid|
  begin
    p = Project.find_by_id(pid)
    next say.("ERROR  webhook project #{pid}: no such project") unless p
    name = "webhook #{p.full_path} -> #{want_url}"
    hooks = p.hooks.select { |h| h.url == want_url }
    next say.("ERROR  #{name}: #{hooks.size} hooks with that URL, want one") if hooks.size > 1
    h = hooks.first
    if h.nil?
      next say.("DRIFT  #{name}: missing") if check
      next say.("ERROR  #{name}: missing, and no token to create it with") if tok.empty?
      p.hooks.create!(want.select { |k, _| ProjectHook.column_names.include?(k.to_s) }.merge(url: want_url, token: tok))
      next say.("FIXED  #{name}: created")
    end
    diff = want.select { |k, v| h.has_attribute?(k) && (!!h.public_send(k)) != v }
    diff[:token] = tok if !tok.empty? && !ActiveSupport::SecurityUtils.secure_compare(h.token.to_s, tok)
    if diff.empty?
      say.("ok     #{name}: pipeline+job events, ssl off" + (tok.empty? ? " (token not checked)" : ", token matches Vault"))
      next
    end
    shown = diff.map { |k, v| k == :token ? "token" : "#{k}=#{!v}->#{v}" }.join(", ")
    next say.("DRIFT  #{name}: #{shown}") if check
    h.update!(diff)
    say.("FIXED  #{name}: #{shown}")
  rescue => e
    say.("ERROR  webhook project #{pid}: #{e.class}: #{e.message[0, 200]}")
  end
end
RUBY
)

# stdin: line 1 the token, then the program. Neither touches a command line.
if ! hook_out=$(printf '%s\n%s\n' "$hook_token" "$HOOK_RUBY" |
    gitlab_exec -i gitlab-rails runner '$hook_token = STDIN.gets.to_s.chomp; eval(STDIN.read)' 2>/dev/null); then
  echo "ERROR  webhooks: gitlab-rails runner failed (needs the gitlab container: run on the router, or set ROUTER_SSH_KEY)"
  errors=$((errors + 1))
fi
unset hook_token
hook_out=$(grep -E '^(ok|DRIFT|FIXED|ERROR) ' <<<"${hook_out:-}" || true)
if [ -n "$hook_out" ]; then
  echo "$hook_out"
  drift=$((drift + $(grep -cE '^(DRIFT|FIXED) ' <<<"$hook_out" || true)))
  errors=$((errors + $(grep -c '^ERROR ' <<<"$hook_out" || true)))
fi

if $check; then
  echo "${drift} setting(s) differ, ${errors} error(s)"
else
  echo "${drift} setting(s) fixed, ${errors} error(s)"
fi
[ "$errors" -eq 0 ] || exit 1
if $check && [ "$drift" -gt 0 ]; then exit 3; fi
exit 0
