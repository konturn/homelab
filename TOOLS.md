# TOOLS.md - Quick Reference Index

Details have moved to focused files. Read what you need, not everything.

## Files

| File | What's in it |
|------|-------------|
| `tools/jit-lib.sh` | Sourceable JIT helper — `jit_get`, `jit_service_key`, `vault_login`, etc. |
| `tools/services.md` | Service access patterns, env vars vs JIT, SSH cookbook, Loki queries |
| `tools/infra-map.md` | Network topology, host IPs, devices, cameras, speakers |

## Noah's Contact Info

**Telegram Chat ID:** `8531859108`
```
message tool: action=send, channel=telegram, to=8531859108
```

## Social Accounts

**Aclawdemy:** prometheus (ID: ebd0c9d7-f300-4ca8-826e-5f0908fba547), API: `$ACLAWDEMY_API_KEY`
**Moltbook:** Prometheus, creds at `memory/moltbook-credentials.json`, use `--location-trusted` with curl

## GitLab (Quick)

Instance: `https://gitlab.lab.nkontur.com` | User: moltbot | Project ID: 4 | Token: `$GITLAB_TOKEN`
Shared lib: `source /home/node/.openclaw/workspace/skills/gitlab/lib.sh`
**Self-merge policy:** Small config tweaks = self-merge. Big architectural changes = get Noah's review.

## Email

Gmail env vars removed. Use JIT T1 `gmail` resource, or `skills/imap-email/SKILL.md`.

## SSH (Persistent Keys)

Key: `/home/node/.openclaw/workspace/.ssh/id_ed25519`
Symlink after restart: `ln -sf /home/node/.openclaw/workspace/.ssh ~/.ssh`
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC6Zp0OU50mhMJvZmiECrSZlq9qvpss+W5gmCsRMuNi1 prometheus@moltbot
```

## Job Machines (OE - KEEP ISOLATED)

| Job | IP | User |
|-----|-----|------|
| J1 | 10.4.128.21 | nkontur |
| J2 | 10.4.128.22 | konoahko |
| J3 | 10.4.128.23 | konturn |

**🚨 CRITICAL:** Never leak cross-job info. Sterile prompts only. Always use subagents.
