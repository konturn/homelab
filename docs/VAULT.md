# Vault authentication

Vault (`https://vault.lab.nkontur.com:8200`, router container `vault`) holds
every service secret under the `homelab/` KV v2 mount. Policies, auth methods
and roles are Terraform in `terraform/vault/`, applied by `vault:configure`.
This page covers who logs in to Vault and how. Secret layout and rotation are
in [SECRET_ROTATION.md](SECRET_ROTATION.md).

No routine path uses the root token: not CI, not the router helpers, not
OpenClaw. Each consumer has its own role and policy.

## Consumers

| Consumer | Auth | Role | Policy (what it can do) |
|---|---|---|---|
| `vault:configure` (Terraform apply) | JWT, `id_tokens` `VAULT_ID_TOKEN` | `vault-admin` | auth methods, policies, mounts, audit (enable only), `homelab/*`, `ssh-client-signer/*`; seal, step-down, rekey and generate-root denied; 15 min |
| `vault:rotate-approles` (manual) | JWT | `vault-admin` | as above: new secret_ids for `openclaw`, `jit-approval-svc`, `router-helpers` |
| `vault:validate` (MR, `terraform plan`) | JWT | `vault-read` | read auth/policy/mount config; no secrets; any branch |
| Deploys (`*:deploy`, `*:deploy-service`, via `fetch-vault-secrets`), drift and report jobs (`netgear:*`, `aruba-*`, `amcrest:drift`, `slzb:drift`, `ups-card:drift`, `report:*`), `gitlab:settings` | JWT | `ci-deploy` | read `homelab/data/*`, write only `homelab/data/docker/zwave-js-ui`; 5 min |
| Home Assistant config repo CI (`root/homeassistant`) | JWT | `ha-config-deploy` | HA token, its bot token, SSH cert for `ha-config` |
| Root's helpers on the router (`haapi`, `hews`, `scripts/gitlab-settings.sh`, `scripts/set-deadman-ping-url.sh`, hand-run `amcrestctl.py`/`slzbctl.py`) | AppRole via `vault-router-token` | `router-helpers` | read the HA token, `docker/event-router`, `cameras/rear`, `infrastructure/slzb`; write `monitoring/healthchecks`; 5 min, bound to 10.3.0.2 |
| OpenClaw | AppRole (`VAULT_APPROLE_*` CI variables) | `openclaw` | `openclaw-ops`: its own config and the JIT API key; everything else through the JIT broker |
| jit-approval-svc | AppRole (Vault KV `docker/jit-approval-svc`) | `jit-approval-svc` | mints scoped short-lived tokens (docs/jit-access-design.md) |

Every JWT role is bound to the `aud` `https://vault.lab.nkontur.com:8200`
and to a project. `ci-deploy`, `vault-admin` and `ha-config-deploy` also
require `ref=main`, `ref_type=branch` and `ref_protected=true`, so an MR or a
branch pipeline cannot log in with them. A job needs an `id_tokens:` block to
get a JWT:

```yaml
id_tokens:
  VAULT_ID_TOKEN:
    aud: https://vault.lab.nkontur.com:8200
```

and logs in with `POST /v1/auth/jwt/login {"role": "...", "jwt": "$VAULT_ID_TOKEN"}`.
The repo's Python tools (`networking/*/…ctl.py`, `ci/reports/run_remote.py`)
do this themselves when `VAULT_TOKEN` is unset, with `VAULT_JWT_ROLE`
(default `ci-deploy`).

There is no `VAULT_TOKEN` CI variable. A job that needs Vault must log in as
above.

## Router helpers: `vault-router-token`

`/usr/local/sbin/vault-router-token` (source `scripts/vault-router-token`)
logs in with the `router-helpers` AppRole and prints a 5-minute token on
stdout:

```sh
VAULT_TOKEN=$(vault-router-token) vault kv get -field=token homelab/docker/homeassistant
```

- The credential is `/etc/vault/router-helpers.json` (root, 0600, directory
  0700). The router deploy (`configure-homelab-services`) writes it from Vault
  KV `homelab/infrastructure/router-helpers`.
- Logins and tokens are accepted only from 10.3.0.2, the router's address on
  the lab VLAN as Vault sees it. A copy of the file is useless elsewhere.
- Rotate with the manual `vault:rotate-approles` job, then run `router:deploy`
  (it rewrites the file). Old secret_ids stay valid until destroyed
  (`vault write auth/approle/role/router-helpers/secret-id-accessor/destroy
  secret_id_accessor=...`).
- A helper that needs another path: add the exact path to the
  `router-helpers` policy in `terraform/vault/policies.tf`. Do not widen it
  to a glob.

## Admin work by hand

Anything the scoped roles cannot do (disabling an audit device, unsealing
by hand, a Vault rebuild) needs the root token. It is a break-glass secret,
kept outside the repo, CI and the routine helper paths. After use, unset it
from the shell. For a lost root token, generate a new one with a quorum of
unseal keys (`vault operator generate-root`).

On a re-initialised Vault (DISASTER_RECOVERY.md, Phase 4.5 Case B) the JWT
roles do not exist yet, so the first `terraform/vault` apply is run by hand
with the new root token; after that `vault:configure` takes over.
