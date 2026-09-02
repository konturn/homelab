# Ansible role for Restic

This role will setup [Restic](https://restic.net/) backups on a Debian/Ubuntu machine using a systemd service and timer.

It supports S3 backend or SFTP backend and will thus setup the SSH config and SSH private keys (see variables below).

## Role Variables

### Restic installation

The role will download and install the restic binary (version `restic_version`) into `restic_path` if the file does not exist.

If you want to force the installation, overwrite the binary or update restic, you can run ansible with `--extra-vars restic_install=true`.

### Restic configuration

- `restic_user`: user to run restic as (`root`)
- `restic_user_home`: home directory of the restic_user (`/root`)
- `restic_password`: password used for repository encryption
- `restic_repository_name`: the name of the repository (`restic`)
- `restic_check`: run `restic check` as `ExecStartPre` if true (`false`)
- `restic_default_folders`: a default list of folders that restic will backup (`/etc/`, `/root` and `/var/log`)
- `restic_folders`: the list of folder you want to backup
- `restic_dump_compression_enabled`: enable piping to pigz for database dumps

Each folder has a `path` and an `exclude` property (which defaults to nothing). The `exclude` property is the literal argument passed to restic (exemple: `--exclude .cache --exclude .local`).

`restic_default_folders` and `restic_folders` are combined to form the final list of backuped folders.

- `restic_global_excludes`: a list of `--exclude` patterns appended to *every* folder backup

Entries without a slash match a file or directory name at any depth, so
`*.db-wal` excludes SQLite write-ahead-log sidecars everywhere. Two things live
here for different reasons: paths that must never leave the host (the Vault
unseal keys), and paths that churn fast enough that restic scans them and then
finds them gone. The second kind matters more than it looks -- see the note on
`SuccessExitStatus` below.

### Partial reads and the ExecStart sequence

`restic-backup.service` is `Type=oneshot` with one `ExecStart` line per folder,
one per database dump, and `forget`/`prune` as `ExecStartPost`. systemd runs
those in order and **stops at the first failure**, skipping everything after it,
including `ExecStartPost`.

restic exits `3` when it wrote the snapshot but could not read at least one
source file. Left alone that turns a warning about one vanished temp file into a
silently truncated backup run. The unit therefore sets `SuccessExitStatus=3` so
the sequence continues. It stays visible through the `restic-backup-incomplete`
Grafana alert (`docker/grafana/provisioning/alerting/infrastructure.yml`), which
matches restic's warning text in the journal.

When that alert fires, find the paths with:

```bash
journalctl -u restic-backup.service --since '24 hours ago' | grep -E '^error:'
```

A path that recurs belongs in `restic_global_excludes`, not in the alert's
noise floor.

- `restic_databases`: a list of databases to dump

Each database has a `name` property which will be the name of the restic snapshot (`{{ database.name }}.sql`). They also have a `dump_command` property which is the command to dump the database to stdout (like `mysqldump dbname`).

- `restic_forget`: run `restic forget` as `ExecStartPost` with `--keep-within {{ restic_forget_keep_within }}` (`true`)
- `restic_forget_keep_within`: period of time to use with `--keep-within` (`30d`)
- `restic_prune`: run `restic prune` as `ExecStartPost` (`true`)

### SSH/SFTP backend configuration

The SSH configuration will be written in `{{ restic_user_home }}/.ssh/config`.

- `restic_ssh_host`: backend name and SSH alias for the backup host
- `restic_ssh_user`: user for SSH connection
- `restic_ssh_hostname`: actual SSH hostname of the backup machine
- `restic_ssh_private_key`: private SSH key used to connect to the backup host
- `restic_ssh_private_key_path`: path of the private key to use (`~/.ssh/backup`)
- `restic_ssh_port`: SSH port to use with the backup machine (`23`)

### S3 backend configuration

- `restic_ssh_enabled`: set to false
- `restic_repository_name`: set to s3 endpoint + bucket, restic syntax (e.g. `s3:https://s3.fr-par.scw.cloud/restic-bucket`)
- `restic_aws_access_key_id`: `AWS_ACCESS_KEY_ID`
- `restic_aws_secret_access_key`: `AWS_SECRET_ACCESS_KEY`

### Sytemd service and timer

A `restic-backup.service` service will be created with all the parameters defined above. The service is of type `oneshot` and will be triggered periodically with `restic-backup.timer`.

The timer is configurable as follows:

- `restic_systemd_timer_on_calender`: defines the `OnCalendar` directive (`*-*-* 03:00:00`)
- `restic_systemd_timer_randomized_delay_sec`: Delay the timer by a random amount of time between 0 and the specified time value. (`0`)

When `restic_check` is true a second pair of units, `restic-check.service` and
`restic-check.timer`, runs a periodic deep integrity check:

- `restic_check_timer_on_calendar`: `OnCalendar` for the deep check (`Sun *-*-* 09:00:00`)
- `restic_check_read_data_subset`: fraction of pack files to re-read and verify (`5%`)
- `restic_check_retry_lock`: how long `check` waits for the repository lock before failing (`2h`)

Keep the check window clear of `restic_systemd_timer_on_calender`. A restic
repository takes a single lock, so a check that starts while the backup is
still running cannot proceed; `restic_check_retry_lock` is the safety net for
a backup that overruns, not a substitute for non-overlapping schedules.

See the [systemd.timer](https://www.freedesktop.org/software/systemd/man/systemd.timer.html) documentation for more information.

You can see the logs of the backup with `journalctl`. (`journalctl -xefu restic-backup`).

## Example playbook

```yaml
---

- hosts: myhost
  roles: restic
  vars:
    restic_ssh_user: backupuser
    restic_ssh_hostname: storage-server.infra.tld
    restic_folders:
      - {path: "/srv"}
      - {path: "/var/www"}
    restic_databases:
    - {name: website, dump_command: sudo -Hiu postgres pg_dump -Fc website}
    - {name: website2, dump_command: mysqldump website2}
    restic_password: mysuperduperpassword
    restic_ssh_private_key: |-
      -----BEGIN OPENSSH PRIVATE KEY-----
      b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
      QyNTUxOQAAACAocs5g1I4kFQ1HH/YZiVU+zLhRDu4tfzZ9CmFAfKhL2AAAAJi02XEwtNlx
      MAAAAAtzc2gtZWQyNTUxOQAAACAocs5g1I4kFQ1HH/YZiVU+zLhRDu4tfzZ9CmFAfKhL2A
      AAAEADZf2Pv4G74x+iNtuwSV/ItnR3YQJ/KUaNTH19umA/tChyzmDUjiQVDUcf9hmJVT7M
      uFEO7i1/Nn0KYUB8qEvYAAAAE3N0YW5pc2xhc0BtYnAubG9jYWwBAg==
      -----END OPENSSH PRIVATE KEY-----
```

S3 example:

```yaml
---

- hosts: myhost
  roles: restic
  vars:
    restic_ssh_enabled: false
    restic_repository: "s3:https://s3.fr-par.scw.cloud/restic-bucket"
    restic_aws_access_key_id: xxxxx
    restic_aws_secret_access_key: xxxxx
    restic_folders:
      - {path: "/srv"}
      - {path: "/var/www"}
    restic_databases:
    - {name: website, dump_command: sudo -Hiu postgres pg_dump -Fc website}
    - {name: website2, dump_command: mysqldump website2}
    restic_password: mysuperduperpassword
```

Of course, `restic_password` and `restic_ssh_private_key` should be stored using ansible-vault.

## License

MIT

## Author Information

See my other Ansible roles at [angristan/ansible-roles](https://github.com/angristan/ansible-roles).
