# APC AP9631 (UPS network management card)

`apcctl.py` keeps the card's `config.ini` as code (`apc-config.ini`,
sanitized) with a daily read-only drift check; `snmpv3-check.sh` tests an
SNMPv3 credential set read-only. Status, setup steps (card credentials are
not in Vault yet) and the SNMP credentials runbook: [docs/POWER.md](../../docs/POWER.md).

```bash
python3 -m unittest discover -s networking/ups -p 'test_*.py' -v
python3 networking/ups/apcctl.py check
```
