# blog.nkontur.com

The blog is a **static site**. The external nginx serves plain files from
`docker/nginx/sites/blog.nkontur.com/`, which Ansible syncs to
`/persistent_data/application/nginx/webroot/blog.nkontur.com/` (the same task
that deploys `nkontur.com`). No PHP is reachable from the internet: the vhost
in `docker/nginx/router.lab.nkontur.com/external_http.conf` has no upstream,
and any `*.php` or `/wp-admin` path returns 404.

The files are an export of the WordPress site that used to serve the blog.
WordPress (compose service `blog`, container `wordpress`) and its MySQL
(`wordpress_db`) are kept, stopped, under the compose profile `wordpress`, so
normal deploys never start them. Their data is untouched:

| Path | Contents |
|---|---|
| `/persistent_data/application/wordpress/html/` | WordPress core, `wp-content/`, `wp-config.php` |
| `/persistent_data/application/wordpress/config/` | legacy config file, not loaded by `wp-config.php` |
| `/persistent_data/application/wordpress/backups/` | DB dumps taken by the reconcile step |
| docker volume `docker_wordpress_db` | MySQL data directory |
| `/mpool/backups/wordpress-final-2026-10-28/` | final DB dump and files archive taken when the site went static |

## What the static site covers

`export_static.py` crawls the site as an anonymous visitor would, starting at
the home page, the sitemaps and the feeds: every post, page, date, category
and author archive, the RSS feeds (`<path>/feed/index.xml`), the sitemaps,
`robots.txt`, the theme's 404 page, and every same-site CSS, JS and image
those reference. URLs are unchanged (`/index.php/YYYY/MM/DD/<slug>/`), so old
links keep working; a path without the trailing slash gets a 301, and the old
`?p=<id>` shortlinks redirect through a `map` in `external_http.conf`.
Drafts and private posts are never exported. Comments and search do not work.

## Editing and re-exporting

Run on the router, from a checkout of this repo:

```sh
docker/wordpress/edit-session.sh start
```

This starts `blog` and `wordpress_db` and a throwaway TLS proxy
(`blog-edit-proxy`, `edit-proxy.conf`) on the lab LAN network with the
existing `*.nkontur.com` certificate. It prints the proxy's address. On the
editing machine, map `blog.nkontur.com` to that address in `/etc/hosts`, log in
at `https://blog.nkontur.com/wp-admin/`, and edit. The proxy only accepts
`10.0.0.0/8`, and the public nginx still serves the static files to everyone
else.

Then:

```sh
docker/wordpress/edit-session.sh export /tmp/blog-static-new
rsync -a --delete /tmp/blog-static-new/ docker/nginx/sites/blog.nkontur.com/
docker/wordpress/edit-session.sh stop
```

Remove the `/etc/hosts` entry, check `git status` (new posts add files under
`index.php/` and `wp-content/uploads/`), and update the `$blog_shortlink` map in
`external_http.conf` from the shortlink lines the export prints. Open an MR.
The deploy syncs the files and reloads nginx.

While WordPress is running, a router deploy also runs the
`configure-wordpress` reconcile below. When it is stopped, the role skips.

## Declared state (when WordPress runs)

`site.yml` declares the core version, the complete theme list (with
versions and the active theme), the complete plugin list, and the
must-use plugins in `mu-plugins/`. Ownership and modes are declared in
`ansible/roles/configure-wordpress/defaults/main.yml`.

Core files live in the bind-mounted `html/` directory, so the image tag does
not change them. The image's entrypoint only copies core into an empty
directory. Core comes from the reconcile step and the `core_version` pin, not
from the image. Keep the image tag on the same release as the pin.

### Reconcile

`ansible/roles/configure-wordpress` runs on every router deploy, after compose
is up, and skips when the `wordpress` container is not running. It uses a
pinned, checksum-verified wp-cli inside the container (as www-data) and:

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

### Changing versions

- **Core:** bump `core_version` in `site.yml`. In the same MR, bump the
  `blog` image tag and digest in `docker/docker-compose.yml` to that release.
  The image update job opens image-only MRs for `blog`. These are not on the
  auto-merge allowlist, so add the `core_version` bump to that MR before
  merging. The reconcile log warns when the two differ.
- **Theme or plugin:** edit the version in `site.yml`. To add one, add an
  entry. To remove one, delete its entry.
- **Must-use plugin:** add or edit the file in `mu-plugins/` and list it under
  `mu_plugins`.

### Manual wp-cli

```sh
docker exec -u 33 -e HOME=/tmp wordpress php /tmp/wp-cli.phar --path=/var/www/html <command>
```

The image has no mysql client, so take DB dumps from `wordpress_db` with
`mysqldump` (see `ensure_backup` in the role's `files/wp-reconcile.py`).

## Restoring from the final backup

`/mpool/backups/wordpress-final-2026-10-28/` holds `wordpress-db.sql.gz`
(`mysqldump --databases wordpress`), `wordpress-files.tar.gz` (the whole
`/persistent_data/application/wordpress/` tree, numeric owners) and
`SHA256SUMS`. To rebuild from it: extract the archive into
`/persistent_data/application/`, start `wordpress_db` under the profile, and
pipe the dump into `mysql` inside it with the root password from the
environment.
