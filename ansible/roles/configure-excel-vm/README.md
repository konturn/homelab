# configure-excel-vm

Windows 11 Pro x64 libvirt/KVM guest on the router for Excel/VBA client work
(must behave like the client's x64 Windows + Microsoft 365 Excel).

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
