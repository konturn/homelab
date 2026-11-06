#!/bin/bash
# Read-only check that an SNMPv3 credential set works against the AP9631.
# Used by the SNMP credentials runbook (docs/POWER.md) before and after each
# step; it only sends SNMP GETs (sysDescr, ups.status-equivalent OIDs).
#
#   SNMP_USER=apc2 SNMP_AUTH_PROTO=SHA SNMP_PRIV_PROTO=AES \
#   SNMP_AUTH_PASS=... SNMP_PRIV_PASS=... networking/ups/snmpv3-check.sh
#
# or with the passwords taken from Vault (needs VAULT_ADDR/VAULT_TOKEN):
#
#   SNMP_USER=apc2 SNMP_AUTH_PROTO=SHA SNMP_VAULT_PATH=homelab/infrastructure/snmp \
#   SNMP_VAULT_AUTH_FIELD=auth_password SNMP_VAULT_PRIV_FIELD=priv_password \
#   networking/ups/snmpv3-check.sh
#
# Passwords never go on a command line: they are written to a 0600
# snmp.conf in a private temp dir (SNMPCONFPATH) that is removed on exit.
set -euo pipefail

HOST="${APC_HOST:-10.4.128.8}"
SNMP_USER="${SNMP_USER:-apc1}"
SNMP_AUTH_PROTO="${SNMP_AUTH_PROTO:-MD5}"
SNMP_PRIV_PROTO="${SNMP_PRIV_PROTO:-AES}"

if [ -n "${SNMP_VAULT_PATH:-}" ]; then
    SNMP_AUTH_PASS="$(vault kv get -field="${SNMP_VAULT_AUTH_FIELD:-password}" "$SNMP_VAULT_PATH")"
    SNMP_PRIV_PASS="$(vault kv get -field="${SNMP_VAULT_PRIV_FIELD:-password}" "$SNMP_VAULT_PATH")"
fi
: "${SNMP_AUTH_PASS:?set SNMP_AUTH_PASS or SNMP_VAULT_PATH}"
: "${SNMP_PRIV_PASS:?set SNMP_PRIV_PASS or SNMP_VAULT_PATH}"

dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT
chmod 700 "$dir"
umask 077
{
    echo "defVersion 3"
    echo "defSecurityName $SNMP_USER"
    echo "defSecurityLevel authPriv"
    echo "defAuthType $SNMP_AUTH_PROTO"
    echo "defPrivType $SNMP_PRIV_PROTO"
    echo "defAuthPassphrase $SNMP_AUTH_PASS"
    echo "defPrivPassphrase $SNMP_PRIV_PASS"
} > "$dir/snmp.conf"
unset SNMP_AUTH_PASS SNMP_PRIV_PASS

# sysDescr, upsBasicOutputStatus (2 = onLine), upsAdvBatteryRunTimeRemaining
if out="$(SNMPCONFPATH="$dir" MIBS='' snmpget -t 5 -r 1 -Oqv "$HOST" \
        .1.3.6.1.2.1.1.1.0 .1.3.6.1.4.1.318.1.1.1.4.1.1.0 .1.3.6.1.4.1.318.1.1.1.2.2.3.0 2>&1)"; then
    echo "OK  user=$SNMP_USER auth=$SNMP_AUTH_PROTO priv=$SNMP_PRIV_PROTO"
    while IFS= read -r line; do echo "    $line"; done <<< "$out"
else
    echo "FAIL user=$SNMP_USER auth=$SNMP_AUTH_PROTO priv=$SNMP_PRIV_PROTO: $out" >&2
    exit 1
fi
