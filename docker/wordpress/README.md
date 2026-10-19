# blog.nkontur.com (WordPress)

Compose service `blog` (container `wordpress`) with `wordpress_db` (MySQL).
Data lives under `/persistent_data/application/wordpress/`:

| Path | Mounted at | Contents |
|---|---|---|
| `html/` | `/var/www/html` | WordPress core, `wp-content/`, `wp-config.php` |
| `config/` | `/etc/wordpress` | legacy config file, not loaded by `wp-config.php` |
| `backups/` | — | DB dumps taken by the reconcile step (last 5 kept) |

## Declared state

`site.yml` declares the core version, the complete theme list (with
versions and the active theme), the complete plugin list, and the
must-use plugins in `mu-plugins/`. Ownership and modes are declared in
`ansible/roles/configure-wordpress/defaults/main.yml`.

Core files live in the bind-mounted `html/` directory, so the image tag does
not change them. The image's entrypoint only copies core into an empty
directory. Core comes from the reconcile step and the `core_version` pin, not
from the image. Keep the image tag on the same release as the pin.

## Reconcile

`ansible/roles/configure-wordpress` runs on every router deploy, after compose
is up. It uses a pinned, checksum-verified wp-cli inside the container (as
www-data) and:

1. Updates core to `core_version` if it differs, then runs `core update-db`.
   It also runs `update-db` if the DB schema is behind the code.
2. Installs each declared theme at its version (`--force` only when the version
   differs), activates `active_theme`, and deletes undeclared themes.
3. Installs and activates declared plugins. Every other plugin is deactivated
   and deleted, and the log names each one.
4. Copies `mu-plugins/` files verbatim and removes undeclared ones.
5. Sets ownership (33:33) and modes from the host: dirs 755, files 644,
   `wp-config.php` 640, and `config/` 750/640.
6. Runs `wp core verify-checksums`. The deploy fails if it does not pass.

Before the first core, theme or plugin change in a run, it writes a DB dump to
`backups/`. When nothing differs, the step reports `changed=0`. In MR
pipelines (`--check`) it lists the changes it would make.

`WORDPRESS_CONFIG_EXTRA` in compose sets `DISALLOW_FILE_MODS`,
`DISALLOW_FILE_EDIT` and `AUTOMATIC_UPDATER_DISABLED`. The admin UI can't
install, update or edit code, so git stays the source of truth.
`DISALLOW_FILE_MODS` is skipped under wp-cli so the reconcile step can apply
changes.

## Changing versions

- **Core:** bump `core_version` in `site.yml`. In the same MR, bump the
  `blog` image tag and digest in `docker/docker-compose.yml` to that release.
  The image update job opens image-only MRs for `blog`. These are not on the
  auto-merge allowlist, so add the `core_version` bump to that MR before
  merging. The reconcile log warns when the two differ.
- **Theme or plugin:** edit the version in `site.yml`. To add one, add an
  entry. To remove one, delete its entry.
- **Must-use plugin:** add or edit the file in `mu-plugins/` and list it under
  `mu_plugins`.

## Manual wp-cli

```sh
docker exec -u 33 -e HOME=/tmp wordpress php /tmp/wp-cli.phar --path=/var/www/html <command>
```

The image has no mysql client, so take DB dumps from `wordpress_db` with
`mysqldump` (see `ensure_backup` in the role's `files/wp-reconcile.py`).
