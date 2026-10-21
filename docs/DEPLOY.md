# Host deploys (router, zwave, satellite-2, macmini)

Every push to `main` runs `router:deploy`, `zwave:deploy`, `satellite-2:deploy`
and `macmini:deploy`. Each one runs
`ci/deploy-scope/deploy_scope.py run --playbook <name>`, which runs
`ansible/<name>.yml` against its host, in full or limited to the roles whose
inputs changed.

## Changed-only deploys

1. **Base commit.** The job reads `/var/lib/homelab-deploy/<playbook>.sha` on
   the host. The last successful deploy of that playbook wrote it. The base is
   not `CI_COMMIT_BEFORE_SHA`: a deploy skipped by the resource group
   (`newest_ready_first`) or a failed deploy would then lose its changes. The
   marker is only written after `ansible-playbook` succeeds.
2. **Changed paths.** `git diff --name-only --no-renames <base> <commit>`. This
   is a tree diff, so merges, several merges in one push and force pushes all
   work.
3. **Rules.** Each path is matched against `ci/deploy-scope/scope.yml`, and the
   first matching rule decides:

   | order | rule | result |
   |---|---|---|
   | 1 | `shared.full`: `.gitlab-ci.yml`, `ci/deploy-scope/**`, `requirements.txt`, `ansible/requirements.yml`, `ansible/ansible.cfg`, `ansible/inventory.yml`, `ansible/known_hosts`, `ansible/{group,host}_vars/**`, `ansible/roles/fetch-vault-secrets/**`, `base/validate/**` | full deploy |
   | 2 | the playbook itself (`ansible/<playbook>.yml`) | full deploy |
   | 3 | `shared.none`: docs, READMEs, terraform, automerge/reports code, switch/AP config, other playbooks, ansible tests, unrelated scripts | no effect |
   | 4 | `ansible/roles/<role>/**` for a role the playbook lists | that role |
   | 5 | `playbooks.<playbook>` rules (repo files a role reads, see below) | the listed roles |
   | 6 | `ansible/roles/**` (roles this playbook does not use) | no effect |
   | 7 | anything else | **full deploy** |

4. **Run.**
   - full: `ansible-playbook ansible/<playbook>.yml`, as before;
   - some roles: `ansible-playbook ... --tags <role>,<role>`. Every role entry in
     the playbooks is tagged with its own name;
   - no roles: no ansible run ("nothing to deploy"). The marker still moves to
     the new commit.

   The job log prints every changed path, the rule that matched it and the
   final decision.

### Router path map (`playbooks.router` in scope.yml)

| paths | roles |
|---|---|
| `docker/docker-compose.yml` | configure-homelab-services, configure-wordpress |
| `docker/wordpress/**` | configure-wordpress |
| `docker/daemon.json` | configure-docker, configure-homelab-services |
| `docker/plex/convert_isos_makemkv.sh` | configure-router-network |
| satellite compose files, `docker/promtail/satellite-config.yml`, `docker/telegraf/satellite-telegraf.conf` | none |
| everything else under `docker/**` | configure-homelab-services |
| `base/nut/**`, `scripts/grafana-token-cleanup.sh` | configure-homelab-services |
| `base/disk/**`, `base/gitlab-runner/**`, `base/systemd/zpool-scrub.*`, `base/udev/**`, `networking/nfs/**` | configure-base |
| `base/msmtp/**` | configure-notifications |
| `networking/{avahi,dhcp,dns,iproute2,iptables,netplan,networkd,ssl}/**` | configure-router-network |

The satellites map their compose and config files to `deploy-compose`,
`base/minidsp` to `configure-zwave`, `snapclient/` to `configure-snapclient`,
`base/sane` and `shell2http.service` to `configure-scanner`, and
`docker/daemon.json` to `configure-docker` and `deploy-compose`. They ignore
the rest of `docker/`, `base/`, `networking/` and `scripts/`, which only
router roles read. The macmini playbook reads nothing outside its role.

## What always runs

- **fetch-vault-secrets** is tagged `always`. It runs on every deploy that
  runs any role and fails closed as before: a failed login, a failed read, a
  missing required path or a missing or empty required field stops the play
  before any role writes anything.
- The playbook `pre_tasks` (installed-package list) and fact gathering.
- Each role's own safety checks run whenever that role runs, because they
  are tasks of the role: `homelab-config-check` validation, `nginx -t`, the
  post-reload nginx check and restarts (configure-homelab-services), the
  dhcpd config check (configure-router-network), the LUKS keyfile and fstab
  guards (configure-base). A changed input selects its role, so those checks
  run for that change.

## Full deploys

A full deploy runs when:

- `DEPLOY_FULL=1` is set, for example from *Run pipeline* or with
  `git push -o ci.variable=DEPLOY_FULL=1`;
- the pipeline was not started by a push (schedule, *Run pipeline*, API).
  Set `DEPLOY_SCOPE=changed` to get a changed-only run from *Run pipeline*;
- the host has no marker, the marker is unreadable, or the base commit cannot
  be fetched;
- any changed path hits rule 1, 2 or 7;
- a rule names a role the playbook does not have.

**Nightly**: a pipeline schedule on `main` with `SCHEDULED_TASK=full-deploy`
runs a full deploy of every host. This corrects drift in roles that
changed-only deploys skipped. The schedule runs only the deploy jobs: the
other jobs skip scheduled task pipelines.

## Keeping scope.yml correct

`deploy-scope:test` (MR and main, whenever ansible/, docker/, base/,
networking/, snapclient/, scripts/ or the CI file change) checks:

- sample diffs: docs only, a single service, dhcpd, a shared file and an
  unmapped path each give the expected decision;
- every repo file a role reads through `lookup('env', 'CI_PROJECT_DIR')`
  maps to that role, or to full, in every playbook that uses the role;
- every inventory `docker_config` src, compose file and nginx config of each
  host maps to `configure-homelab-services` (router) or `deploy-compose`
  (satellites);
- every role entry in the playbooks is tagged with its name, no role task
  uses the `never` tag, and every role named in scope.yml exists in its
  playbook.

When you add a repo file that a role reads, add it to scope.yml. If you
forget, the path is unmapped and triggers a full deploy, unless it falls
under a broader rule; the test catches literal paths and inventory entries.
A new role must be tagged with its name in the playbook.

Try a diff locally:

```sh
python3 ci/deploy-scope/deploy_scope.py select --playbook router --base origin/main~3
python3 ci/deploy-scope/deploy_scope.py select --playbook zwave docker/daemon.json
```

## Timing

`profile_tasks` prints the 20 slowest tasks at the end of every run.

Full deploys, job duration (2026-10-21):

| job | before (job 25100 etc.) | after !846, !849, !850 |
|---|---|---|
| router:deploy | 366s | 222s |
| zwave:deploy | 216s | 124s |
| satellite-2:deploy | 195s | 104s |
| macmini:deploy | 40s | 28s |

Most of the gain came from collapsing per-item remote loops (Vault reads,
config files, symlinks) and skipping apt calls when packages are present.
