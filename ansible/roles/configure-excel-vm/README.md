# configure-excel-vm

Windows 11 Pro x64 libvirt/KVM test VM on the router for Excel/VBA work
(x64 Windows + 64-bit Microsoft 365 Excel).

| | |
|---|---|
| Domain | `excel-vm` (q35, OVMF Secure Boot, swtpm TPM 2.0, host-passthrough CPU) |
| Resources | 6 vCPU / 12 GiB (hard cap, asserted) |
| Disk | `/persistent_data/vms/excel-vm/excel-vm.qcow2`, 100G qcow2, virtio-blk |
| Network | macvtap bridge on `bond0.3`, MAC `52:54:00:3a:48:0a`, static `10.3.48.10/16`, gw `10.3.0.1`, DNS pihole |
| DNS | `excel-vm.lab.nkontur.com` (pihole_dns_hosts) |
| Access | RDP `10.3.48.10:3389`, SSH `noah@10.3.48.10`, console `virsh console`/VNC on host loopback |
| Credentials | Vault `homelab/excel-vm`, field `admin_password` (user `noah`) |
| Backups | none: `/persistent_data/vms` is outside every restic folder and in `restic_global_excludes` |

## What a normal deploy does

Ensures packages, enables `libvirtd` at boot (so `virsh autostart` fires),
configures `libvirt-guests` to shut guests down cleanly on host shutdown,
downloads the pinned virtio-win ISO, and keeps autostart on. It never defines,
redefines, starts, stops or reinstalls the VM, and never touches its disk.

## Why the static address is in 10.3.48.0/24

A macvtap guest can only talk to the host through `vlan3-shim` (its macvlan
sibling). The host routes only `10.3.0.0/18` via the shim, so a DHCP-pool
address (`10.3.64.0+`, routed out `bond0.3`) would get no internet and no
access from other VLANs. `10.3.48.x` is inside the shim route and clear of
docker's IPAM (first-free from `10.3.0.3`) and the static `10.3.32.x` block.

## Building it (one time)

1. Download **Windows 11 (multi-edition ISO for x64 devices)**, English (United States),
   from https://www.microsoft.com/en-us/software-download/windows11 in a browser
   (Microsoft rejects scripted downloads). Copy it to the router as
   `/persistent_data/vms/iso/Win11_26H2_English_x64.iso` and check
   `sha256sum` matches `excel_vm_windows_iso.sha256` in `defaults/main.yml`.
   If Microsoft has moved on to a newer build, update filename + SHA256 from
   the page's "Verify your download" table in an MR first.
2. Make sure Vault `homelab/excel-vm` has `admin_password` (or export
   `EXCEL_VM_ADMIN_PASSWORD` for a manual run).
3. On the router, from the repo checkout:
   ```bash
   cat > /tmp/excel-vm.yml <<'EOF'
   - hosts: router.lab.nkontur.com
     connection: local
     roles: [configure-excel-vm]
   EOF
   ANSIBLE_ROLES_PATH=ansible/roles EXCEL_VM_ADMIN_PASSWORD=... \
     /usr/bin/ansible-playbook -i ansible/inventory.yml /tmp/excel-vm.yml -e excel_vm_install=true
   ```
   (`/usr/bin/ansible-playbook`: the pip copy in `/usr/local/bin` is broken on this host.)
4. Wait 30-60 min. Progress, from the host:
   ```bash
   virsh qemu-agent-command excel-vm '{"execute":"guest-ping"}'     # agent up = drivers in
   ```
   and `C:\ProgramData\excel-vm-setup\status.json` / `DONE` in the guest.
5. Remove the install media (it contains the password):
   run step 3 again with `-e excel_vm_cleanup_install_media=true` instead.

## Guest settings

Besides the installers, `excel-vm-setup.ps1` sets up the guest for driving
Excel from automation. These steps are marked `-Idempotent`: each checks the
current state, changes only what differs and reports `unchanged` or
`changed: ...`.

| Step | What |
|---|---|
| `power-plan` | High performance power scheme (`excel_vm_power_scheme_guid`) |
| `ssh-admin-keys` | `excel_vm_admin_authorized_keys` (the router's root keys) in `C:\ProgramData\ssh\administrators_authorized_keys`, ACL Administrators + SYSTEM only. `noah` is an administrator, so sshd reads this file, not `~\.ssh`. |
| `sshd-sftp-short-path` | `Subsystem sftp C:\PROGRA~1\OpenSSH\SFTP-S~1.EXE` in `sshd_config`, then restarts sshd. With PowerShell as DefaultShell, sshd starts the subsystem through PowerShell, which breaks both the default relative `sftp-server.exe` and a quoted `C:\Program Files\...` path, so sftp/scp fail. The 8.3 short path (looked up, not hard-coded) works. The first change saves `sshd_config.bak-excel-vm`. |
| `python-user` | Per-user Python `excel_vm_user_python_version` (installer `python-user`) for `noah`, on the user PATH, `PY_PYTHON` set so `py` picks it. The all-users Python from the `python` step stays first on the machine PATH, so a bare `python` is that one: use `py` or `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. |
| `pip-packages-user` | `excel_vm_user_pip_packages` in that Python (xlwings, oletools, openpyxl, pdfplumber, pdfminer.six, pypdf, pandas) |
| `excel-trust-vbom` | Trust access to the VBA project object model (HKCU) |
| `excel-trusted-locations` | `<excel_vm_project_dir>\<d>\` for each `excel_vm_trusted_subdirs` (`work`, `out`) as Excel Trusted Locations with subfolders allowed (HKCU `...\Excel\Security\Trusted Locations\Location*`). An existing location for the same folder is kept, whatever its key name. |
| `excel-com-systemprofile-desktop` | `C:\Windows\System32\config\systemprofile\Desktop` and the `SysWOW64` one. Without them Excel started through COM from SSH (session 0) fails `Workbooks.Open` with "Microsoft Excel cannot access the file". |
| `defender-exclusions` | Defender exclusions: path `excel_vm_project_dir`, processes `excel_vm_defender_exclusion_processes` (`EXCEL.EXE`) |

`excel_vm_project_dir` defaults to the placeholder `C:\work`. Pass the real
project folder with `-e excel_vm_project_dir=...`; it is not kept in this
(public) repo. A first-boot build creates the folders, a re-apply fails the
step instead if they do not exist (wrong `excel_vm_project_dir`).

## Re-applying settings

At build time the script runs once from autounattend (FirstLogonCommands).
To apply the steps above to the running VM, for example after changing one of
them, run the role with `excel_vm_reapply=true` on the router. It renders the
script, copies it over SSH (as `noah`, with the router's root key, host key in
`/root/.ssh/known_hosts_excel_vm`) together with the `excel_vm_reapply_installers`,
and runs it with `-Reapply`: only the `-Idempotent` steps, no installers, no
reboot, nobody signed out. HKCU settings go to `noah`'s hive even while he is
logged in over RDP. If a step changes `sshd_config` it restarts sshd; open
sessions survive that.

```bash
cat > /tmp/excel-vm.yml <<'EOF'
- hosts: router.lab.nkontur.com
  connection: local
  roles: [configure-excel-vm]
EOF
# Dry run: the guest script gets -Check and reports "would change: ..."
ANSIBLE_ROLES_PATH=ansible/roles /usr/bin/ansible-playbook -i ansible/inventory.yml /tmp/excel-vm.yml \
  -e excel_vm_reapply=true -e 'excel_vm_project_dir=C:\<project>' --check
# Apply
ANSIBLE_ROLES_PATH=ansible/roles /usr/bin/ansible-playbook -i ansible/inventory.yml /tmp/excel-vm.yml \
  -e excel_vm_reapply=true -e 'excel_vm_project_dir=C:\<project>'
```

The task prints one `REAPPLY <step>: <result>` line per step and is
`changed` only if a step changed something; any `failed` step fails it.
The guest keeps `C:\ProgramData\excel-vm-setup\reapply.log` and
`reapply-status.json`. Normal deploys (CI) never re-apply.

## Microsoft 365 / Excel

`excel-vm-setup.ps1` installs Microsoft 365 Apps (64-bit, Excel only, Current
Channel) with the Office Deployment Tool and turns on *Trust access to the VBA
project object model* for `noah`. Activation needs a Microsoft account, by
hand: RDP in, open Excel, **Sign in** with the account that holds the M365
licence, accept the "Your account allows editing" prompt (restart Excel if it
asks). Check: File > Account shows "Subscription Product" and Product
Information says 64-bit.

## Rebuilding

The role refuses to install over an existing disk. To rebuild from scratch:
`virsh destroy excel-vm; virsh undefine excel-vm --nvram --tpm`, move the qcow2
away, then follow "Building it".
