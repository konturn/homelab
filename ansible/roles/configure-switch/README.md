# configure-switch

Pushes rendered Aruba S2500-48P (AOS-S) configuration via SSH with automatic rollback safety.

## How it works

1. **Save current config** — `write memory` saves running-config as startup-config
2. **Schedule reload** — `reload after N` schedules a reboot (default: 5 minutes)
3. **Push new config** — Renders the Jinja2 template and pushes via SSH `conf t`
4. **Verify connectivity** — Pings the switch from the router
5. **On success** — Cancels the scheduled reload, runs `write memory` to persist
6. **On failure** — Does nothing; the switch auto-reboots to the saved startup-config

## Dead Man's Switch (Rollback Safety)

This is the critical safety mechanism. Before any config change:

- The current running-config is saved to startup-config via `write memory`
- A timed reload is scheduled (`reload after 5`)
- If the new config breaks SSH/network connectivity, the switch will reboot after the timeout and load the pre-change startup-config
- Only after verifying connectivity does the role cancel the reload and persist the new config

**This means a bad config push can never permanently brick the switch** — worst case, it reboots in 5 minutes and comes back with the previous config.

### Manual recovery

If something goes wrong and you need to intervene:

- **Cancel a pending reload:** SSH to 10.100.0.1 and run `reload cancel`
- **Force rollback:** Let the reload timer expire, or run `reload` manually
- **The backup config** is whatever was running before the Ansible push

## Variables

| Variable | Default | Description |
|---|---|---|
| `switch_host` | `10.100.0.1` | Switch management IP |
| `switch_user` | `admin` | SSH user |
| `switch_config_source` | `networking/switches/aruba-config` | Jinja2 template path |
| `switch_config_rendered` | `<data_path>/ansible_state/aruba-config` | Rendered config path |
| `switch_rollback_timeout_minutes` | `5` | Minutes before dead man's switch reload |
| `switch_connectivity_test_host` | `10.100.0.254` | Host to ping for connectivity verification |

## CI Integration

The `switch:configure` job in `.gitlab-ci.yml` runs this role. It is **manual only** (`when: manual`) for safety — you must click "Play" in the GitLab UI to trigger it.

It runs after `router:deploy` to ensure the rendered config is up to date.

## SSH Access

The CI runner SSHes to the router, and the role uses `delegate_to` to SSH from the router to the switch at 10.100.0.1 on the management network. The switch SSH credentials use the same pattern as `bootstrap.yml`.
