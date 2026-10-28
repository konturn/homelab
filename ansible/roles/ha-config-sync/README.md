# ha-config-sync

Deploy target for the private Home Assistant config repo (`root/homeassistant`
on GitLab), which holds the automations, scripts, scenes and blueprints. This
repo only provides the plumbing; see docs/HOMEASSISTANT.md.

- User `ha-config`: no password, reachable only with an SSH certificate from
  Vault's `ha-config-deploy` role (principal `ha-config-deploy`, 5-minute TTL,
  `terraform/vault/ssh.tf`). The principal line in
  `/etc/ssh/auth_principals/ha-config` forces the command
  `sudo -n /usr/local/sbin/ha-config-sync` with `restrict`, and sudoers allows
  only that script.
- `ha-config-sync` (`files/ha-config-sync`) takes its subcommand from
  `SSH_ORIGINAL_COMMAND`:
  - `deploy` / `check`: a tar.gz of `automations/ scripts/ scenes/ blueprints/`
    on stdin. Refuses duplicate ids (inside the tarball, or against
    `/config/packages/`) and items whose UI-file copy differs. Moves items
    that are identical in the UI files out of them, archives the previous
    state to `/var/backups/ha-config-sync/`, then replaces the three managed
    directories and writes the blueprints (it deletes only blueprints it
    deployed before). Prints a JSON summary with the expected item counts.
    `check` does the same without writing. Neither reloads Home Assistant;
    the CI job calls the reload services over the API.
  - `export`: a tar.gz of the UI files, managed directories, managed
    blueprints and a `meta.json` (counts, package ids, running image). Used
    by the drift job.
  - `status`, `log <unix-seconds>`: counts, and automation/script/scene log
    errors since a time.
- It never reads or prints `secrets.yaml` or `.storage`.

By hand on the router: `sudo ha-config-sync status`.
