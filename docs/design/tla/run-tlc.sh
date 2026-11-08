#!/usr/bin/env bash
# Model-check the event router spec (docs/design/event-routing.md, section 12):
# the base model must pass every property, and every mutation in mutations/
# must fail on the property its cfg names (`\* expect: <property>`).
#
# Usage: run-tlc.sh [ci|full|base|mutations|<cfg name>]   (default: ci)
#   ci     small seed instances (ci-safety, ci-pages, ci-liveness) + every mutation; what
#          CI runs, a few minutes on 4 threads
#   full   the bigger instances (safety, liveness) + every mutation; by hand,
#          ~10 minutes and several GB (numbers in README.md)
#
# Environment:
#   TLA2TOOLS_JAR  tla2tools.jar to use; default: download v$TLA_VERSION into
#                  $TLA_CACHE_DIR (default $TMPDIR or /tmp) and check its sha256
#   TLC_WORKERS    TLC worker threads (default 4; keep it at 4 or less on the router)
#   TLC_XMX        Java heap (default 2g)
# TLC always runs at nice 19 and, where available, ionice idle class: it
# must never compete with the router's real services.
set -euo pipefail

cd "$(dirname "$0")"

TLA_VERSION=1.7.4
TLA_SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
WORKERS=${TLC_WORKERS:-4}
XMX=${TLC_XMX:-2g}
WHAT=${1:-ci}
LOW=(nice -n 19)
if command -v ionice >/dev/null 2>&1 && ionice -c3 true 2>/dev/null; then
  LOW+=(ionice -c3)
fi

JAR=${TLA2TOOLS_JAR:-}
if [ -z "$JAR" ]; then
  mkdir -p "${TLA_CACHE_DIR:=${TMPDIR:-/tmp}}"
  JAR=$TLA_CACHE_DIR/tla2tools-$TLA_VERSION.jar
  if [ ! -f "$JAR" ]; then
    curl -fsSL -o "$JAR.part" \
      "https://github.com/tlaplus/tlaplus/releases/download/v$TLA_VERSION/tla2tools.jar"
    mv "$JAR.part" "$JAR"
  fi
  echo "$TLA_SHA256  $JAR" | sha256sum -c --quiet -
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
LOGDIR=${TLC_LOGDIR:-$WORK/logs}
mkdir -p "$LOGDIR"

failures=0
summary=()

# run_tlc <cfg> -> sets rc and log
run_tlc() {
  local cfg=$1 name
  name=$(basename "$cfg" .cfg)
  log="$LOGDIR/$name.log"
  set +e
  "${LOW[@]}" java -XX:+UseParallelGC -Xmx"$XMX" -cp "$JAR" tlc2.TLC \
    -workers "$WORKERS" -deadlock -cleanup -metadir "$WORK/meta-$name" \
    -config "$cfg" EventRouter.tla >"$log" 2>&1
  rc=$?
  set -e
}

stats() {  # "<distinct> distinct, <time>" from a TLC log
  local d t
  d=$(grep -oE '[0-9]+ distinct states found' "$1" | tail -1 | cut -d' ' -f1)
  t=$(grep -oE 'Finished in [0-9a-z ]+' "$1" | tail -1 | sed -e 's/Finished in //' -e 's/ at *$//')
  echo "${d:-?} distinct states, ${t:-?}"
}

record() { summary+=("$1"); echo "$1"; }

check_base() {
  local cfg=$1
  run_tlc "$cfg"
  if [ "$rc" -eq 0 ] && grep -q 'No error has been found' "$log"; then
    record "PASS  base $(basename "$cfg" .cfg): all properties hold ($(stats "$log"))"
  else
    record "FAIL  base $(basename "$cfg" .cfg): TLC exit $rc (log: $log)"
    grep -E '^Error|violated' "$log" | head -5 || true
    failures=$((failures + 1))
  fi
}

check_mutation() {
  local cfg=$1 name expect
  name=$(basename "$cfg" .cfg)
  expect=$(sed -n 's/^\\\* expect: //p' "$cfg")
  [ -n "$expect" ] || { record "FAIL  mutation $name: no '\\* expect:' line"; failures=$((failures + 1)); return; }
  run_tlc "$cfg"
  # TLC exit codes: 12 = invariant violated, 13 = temporal property violated.
  # Each mutation cfg checks only its expected property, so a violation of
  # the right kind is a violation of that property.
  if [ "$rc" -eq 12 ] && grep -q "Invariant $expect is violated" "$log"; then
    record "PASS  mutation $name: $expect violated, as expected ($(stats "$log"))"
  elif [ "$rc" -eq 13 ] && [[ $expect == L* ]] && grep -q 'Temporal properties were violated' "$log"; then
    record "PASS  mutation $name: $expect violated, as expected ($(stats "$log"))"
  else
    record "FAIL  mutation $name: expected $expect to fail, TLC exit $rc (log: $log)"
    grep -E '^Error|violated|No error' "$log" | head -5 || true
    failures=$((failures + 1))
  fi
}

case "$WHAT" in
  ci|full|base|mutations) ;;
  *) [ -f "$WHAT.cfg" ] || [ -f "mutations/$WHAT.cfg" ] || { echo "unknown cfg: $WHAT" >&2; exit 2; } ;;
esac

if [ "$WHAT" = ci ] || [ "$WHAT" = base ]; then
  check_base ci-safety.cfg
  check_base ci-pages.cfg
  check_base ci-liveness.cfg
elif [ "$WHAT" = full ]; then
  check_base safety.cfg
  check_base liveness.cfg
elif [ -f "$WHAT.cfg" ]; then
  check_base "$WHAT.cfg"
fi

if [ "$WHAT" = ci ] || [ "$WHAT" = full ] || [ "$WHAT" = mutations ]; then
  for cfg in mutations/*.cfg; do check_mutation "$cfg"; done
elif [ -f "mutations/$WHAT.cfg" ]; then
  check_mutation "mutations/$WHAT.cfg"
fi

echo
echo "== summary =="
printf '%s\n' "${summary[@]}"
if [ "$failures" -gt 0 ]; then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
