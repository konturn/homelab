#!/bin/sh
# Emits one zpool_health point per imported pool, in influx line protocol.
#   zpool_health,pool=mpool health="ONLINE",healthy=1i,collect_ok=1i
# healthy is 1 only for ONLINE. DEGRADED, FAULTED, UNAVAIL, SUSPENDED,
# REMOVED and OFFLINE are all 0.
#
# If zpool itself fails, a single point with pool=_collect and collect_ok=0i
# is written instead of nothing. A pool that silently stops reporting would
# otherwise look the same as a healthy one (an empty result, noDataState OK).
#
# zpool list is read-only and works without root on OpenZFS >= 0.7
# (/dev/zfs is 0666), so this runs as the telegraf user with no sudo rule.
ZPOOL=${ZPOOL:-/usr/sbin/zpool}
out=$("$ZPOOL" list -H -o name,health 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$out" ]; then
  echo 'zpool_health,pool=_collect collect_ok=0i,healthy=0i'
  exit 0
fi
printf '%s\n' "$out" | while IFS="$(printf '\t')" read -r name health; do
  [ -n "$name" ] || continue
  if [ "$health" = "ONLINE" ]; then h=1; else h=0; fi
  printf 'zpool_health,pool=%s health="%s",healthy=%si,collect_ok=1i\n' "$name" "$health" "$h"
done
