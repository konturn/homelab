#!/bin/sh
# Fail-closed tests for fetch-vault-secrets. Needs ansible-playbook and
# python3; touches nothing outside a temp dir. Run from anywhere.
set -u
here=$(cd "$(dirname "$0")" && pwd)
roles=$(cd "$here/../../roles" && pwd)
port=${MOCK_VAULT_PORT:-18200}
work=$(mktemp -d)
python3 "$here/mock_vault.py" "$port" &
mock=$!
trap 'kill $mock 2>/dev/null; rm -rf "$work"' EXIT
sleep 1

fail=0
# scenario  expected-result  task-that-must-stop-the-play
while read -r scenario expect stop_task; do
    marker="$work/$scenario.written"
    log="$work/$scenario.log"
    ANSIBLE_ROLES_PATH="$roles" ANSIBLE_NOCOLOR=1 ansible-playbook \
        -i localhost, "$here/test.yml" \
        -e "mock_port=$port scenario=$scenario marker=$marker" >"$log" 2>&1
    rc=$?
    result=pass
    if [ "$expect" = ok ]; then
        [ "$rc" -eq 0 ] && [ -f "$marker" ] || result=FAIL
    else
        [ "$rc" -ne 0 ] && [ ! -f "$marker" ] || result=FAIL
        # The play must stop at the expected task, not somewhere later.
        grep -A12 "TASK \[fetch-vault-secrets : $stop_task\]" "$log" \
            | grep -q -E "^(fatal|failed):" || result=FAIL
    fi
    if grep -q -E "value-(one|two|three|four)" "$log"; then
        echo "  secret value leaked into the log"
        result=FAIL
    fi
    printf '%-14s expect=%-5s rc=%s config-written=%s %s\n' "$scenario" "$expect" "$rc" \
        "$([ -f "$marker" ] && echo yes || echo no)" "$result"
    if [ "$result" != pass ]; then
        fail=1
        sed 's/^/    | /' "$log" | tail -40
    elif [ -n "${VERBOSE:-}" ]; then
        grep -E "^(fatal|failed):|\"msg\"|Required secret|not found \(404\)" "$log" | sed 's/^/    | /'
    fi
done <<'CASES'
ok             ok    -
missing-opt    ok    -
timeout        fail  Fail on Vault read errors
forbidden      fail  Fail on Vault read errors
missing-path   fail  Fail on missing required Vault paths
missing-field  fail  Assert all required secrets are present
empty-field    fail  Assert all required secrets are present
bad-auth       fail  Fail if Vault authentication failed
CASES
exit "$fail"
