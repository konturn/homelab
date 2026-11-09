# ha-config-sync

Deploy target for the private Home Assistant config repo (`root/homeassistant`
on GitLab), which holds the automations, scripts, scenes, blueprints and the
helpers (groups, `input_*` and the like, as packages). This repo only provides
the plumbing; see docs/HOMEASSISTANT.md.

- User `ha-config`: no password, reachable only with an SSH certificate from
  Vault's `ha-config-deploy` role (principal `ha-config-deploy`, 5-minute TTL,
  `terraform/vault/ssh.tf`). The principal line in
  `/etc/ssh/auth_principals/ha-config` forces the command
  `sudo -n /usr/local/sbin/ha-config-sync` with `restrict`, and sudoers allows
  only that script.
- `ha-config-sync` (`files/ha-config-sync`) takes its subcommand from
  `SSH_ORIGINAL_COMMAND`:
  - `deploy` / `check`: a tar.gz of `automations/ scripts/ scenes/ blueprints/
    packages/` on stdin. Refuses duplicate ids (inside the tarball, or against
    `/config/packages/`) and items whose UI-file copy differs. Moves items
    that are identical in the UI files out of them, archives the previous
    state to `/var/backups/ha-config-sync/`, then replaces the three managed
    directories and writes the blueprints (it deletes only blueprints it
    deployed before). `packages/` (flat, one package per file) replaces
    `/config/packages/private/`; a package there may not define
    automations, scripts or scenes, nor reuse the name of a package of this
    repo. Prints a JSON summary with the expected item counts and the
    package keys (integrations) that changed. `check` does the same without
    writing. Neither reloads nor restarts Home Assistant; the CI job calls
    the reload services over the API.
  - `export`: a tar.gz of the UI files, managed directories, managed
    blueprints, private packages and a `meta.json` (counts, package ids and
    file names, running image). Used by the drift job.
  - `status`, `log <unix-seconds>`: counts, and log errors since a time from
    automations/scripts/scenes, config loading and the helper integrations.
- It never reads or prints `secrets.yaml` or `.storage`.

By hand on the router: `sudo ha-config-sync status`.
