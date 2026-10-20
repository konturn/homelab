# Aruba Instant AP cluster

The Wi-Fi cluster (two AP-515s running Aruba Instant, one acting as virtual
controller) is configured from `running-config.conf` in this directory.

The cluster has a static virtual controller address, 10.4.128.10 on the
management VLAN (`virtual-controller-ip`, outside the DHCP pool). It always
answers on the current conductor, so the live tools and CI connect to it
(Vault `homelab/network/aruba-iap#host`), never to an AP's own DHCP address.

| File | Purpose |
|---|---|
| `running-config.conf` | Desired state: the cluster's `show running-config`, normalised |
| `iapctl.py` | Export, plan, apply and drift check over the controller's SSH CLI |
| `test_iapctl.py` | Offline unit tests |
| `known_hosts` | SSH host keys of the APs and of the virtual controller address, which is listed with both APs' keys (either can become the conductor) |

## Secrets

Secret values never appear in git. `export` replaces them with placeholders
of the form `{{vault:<path>#<field>}}`:

| Placeholder path | Fields |
|---|---|
| `homelab/network/aruba-iap` | `host`, `username`, `password` (CLI login). The controller key and admin hash placeholders are not stored; those lines are protected and never sent |
| `homelab/network/aruba-iap-ssids` | one field per SSID profile name, holding the WPA passphrase in clear text |

Placeholders match any live value, so they never show up as a difference.
They are only resolved from Vault when a change has to send that line, for
example a new SSID profile. If the field is missing, `apply` refuses.

The controller stores passphrases encrypted (with a random IV per export),
so a passphrase changed outside git is not detected by the drift check.

## Usage

```sh
# offline: what would change between two files
python3 iapctl.py plan --current old.conf --desired running-config.conf

# live (credentials from IAP_HOST/IAP_USER/IAP_PASS, or VAULT_ADDR + VAULT_TOKEN)
python3 iapctl.py export -o running-config.conf
python3 iapctl.py plan
python3 iapctl.py apply [--dry-run] [--allow-protected]
python3 iapctl.py drift
```

`apply`:

1. exports the live config and diffs it against the desired file;
2. sends only the changed blocks or lines (`configure terminal`, the block
   header, the changed lines, `exit`, then `end` and `commit apply`);
3. re-exports and fails if any difference remains.

An empty diff sends nothing. Changes to the virtual controller address or
VLAN, the uplink and the wired profile assigned to the uplink port, mesh and
extended-SSID, cluster membership, and management access are refused unless
`--allow-protected` is given. Changes to an access rule are refused if the
resulting rule order cannot be reproduced; edit those on the controller and
re-export.

Not managed: firmware (`version` is ignored), per-AP settings (`show ap-env`:
AP names, per-AP uplink VLAN, radio overrides) and secrets as described above.

## CI

| Job | When | Credentials |
|---|---|---|
| `aruba-iap:test` | MRs and main, on changes here | none: unit tests and an offline plan against the file on the target branch |
| `aruba-iap:apply` | main, on changes here | Vault (`ci-deploy`), protected runner |
| `aruba-iap:apply-protected` | main, manual | as above, with `--allow-protected` |
| `aruba-iap:drift` | daily schedule on main with `IAP_TASK=drift` | as above; mails root on drift |

To change the Wi-Fi config, edit `running-config.conf` (or change it on the
controller and run `export`), open an MR, and review the plan in
`aruba-iap:test`.
