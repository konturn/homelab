# Raspberry Pi hosts (zwave, satellite-2)

Both are Raspberry Pi 4B on Raspberry Pi OS bookworm (arm64), remote-only:
if one does not boot, someone has to walk to it. Treat every reboot of the
Z-Wave Pi as a Z-Wave outage of a minute or two.

## Boot layout

| Path | What it is |
|---|---|
| `/boot/firmware` | FAT boot partition (`/etc/fstab`). The firmware reads `config.txt`, `cmdline.txt`, `kernel8.img`, `initramfs8`, the dtbs and `overlays/` from its root |
| `/boot` | ext4, on the root filesystem. Debian kernel files (`vmlinuz-*`, `initrd.img-*`). `config.txt` / `cmdline.txt` there are stubs the firmware never reads |

The kernel is the `linux-image-rpi-v8` package. On each kernel or initramfs
update, `raspi-firmware`'s hooks copy the newest `vmlinuz`/`initrd.img`, the
dtbs and the overlays into `/boot/firmware`. `config.txt` has
`auto_initramfs=1`, so the firmware loads `initramfs8` with `kernel8.img`.
`cmdline.txt` carries `panic=10`, so a kernel panic reboots instead of hanging.

`configure-pi-boot` keeps this state: it fails on a host still on the legacy
layout (`/boot` = FAT partition, the frozen `raspberrypi-kernel` package),
keeps the kernel packages installed, and manages `auto_initramfs=1` and the
`cmdline.txt` parameters (`pi_boot_cmdline_params`). On zwave,
`configure-snapclient` templates the whole `config.txt`
(`snapclient/config.txt`), so that template carries `auto_initramfs=1` and
`dtparam=watchdog=on` itself.

A fresh Raspberry Pi OS bookworm image already has this layout.

Watchdog: `configure-pi-watchdog` (`dtparam=watchdog=on` plus systemd
`RuntimeWatchdogSec`). A hung kernel or PID 1 resets the board.

### Old kernel fallback

Both Pis still carry the last legacy kernel (6.1.21-v8+) as an unused
fallback: the boot set in `/boot/firmware/old-6.1.21/` and its modules in
`/lib/modules/6.1.21-v8+` (not owned by any package). To boot it, add
`os_prefix=old-6.1.21/` and remove `auto_initramfs=1` in `config.txt`. Once the
Debian kernel has been through an update or two, both can be deleted.

## Bootloader EEPROM

`rpi-eeprom-update.service` checks at every boot and stages a newer bootloader
from the `rpi-eeprom` package (`default` release) into `/boot/firmware`; it is
written on the following reboot. `rpi-eeprom-update -r` cancels a staged
update only until the next boot stages it again. Check with
`rpi-eeprom-update` and `vcgencmd bootloader_version`.

## Network

Wired, DHCP with a reservation on the router by MAC (`zwave_mac`,
`satellite_2_address` in the inventory). The upgraded hosts use `dhcpcd`, not
NetworkManager. `raspberrypi-net-mods` is removed: on bookworm it only pulls in
NetworkManager for the desktop Wi-Fi UI, and holding it back blocks upgrades.
A fresh bookworm image uses NetworkManager instead; the reservation gives it
the same address.

## Changing the boot config safely: tryboot

A bad `config.txt`, `cmdline.txt` or kernel can leave a Pi unbootable. The
bootloader's tryboot mode tests a change for one boot only:

1. Write the new config as `/boot/firmware/tryboot.txt`; leave `config.txt`
   as the known-good one.
2. `systemctl reboot "0 tryboot"`. The firmware uses `tryboot.txt` for this
   boot only. `cat /proc/device-tree/chosen/bootloader/tryboot` reads 1.
3. If the boot fails, a reset (watchdog, `panic=10`, or power) boots
   `config.txt` again.
4. When the host checks out, copy `tryboot.txt` over `config.txt`, delete
   `tryboot.txt` and reboot normally once more.

Files the firmware loads by name (kernel, dtbs, overlays, `cmdline.txt`) can be
kept apart with `os_prefix=<dir>/` in one of the two configs.

## Legacy layout to bookworm layout (done 2026-11-07)

How both Pis were moved off `raspberrypi-kernel` 6.1.21 (#134 follow-up), for a
Pi that still has the old layout:

1. Copy the old boot set aside on the FAT partition: `kernel8.img`, the dtbs,
   `overlays/` and `cmdline.txt` into `/boot/old-6.1.21/`.
2. `umount /boot`, change the boot partition's mount point in `/etc/fstab`
   from `/boot` to `/boot/firmware`, `mkdir /boot/firmware`, mount it.
3. Remove DKMS modules that do not build for the new kernel, then
   `apt install raspi-firmware linux-image-rpi-v8`. The hooks put the new
   `kernel8.img`, `initramfs8`, dtbs and overlays into `/boot/firmware`.
4. `tryboot.txt` = the old `config.txt` plus `auto_initramfs=1`; `config.txt`
   = the old one plus `os_prefix=old-6.1.21/` (the fallback). Tryboot as above.
5. After verifying, purge `raspberrypi-kernel`, `raspberrypi-kernel-headers`
   and `raspberrypi-bootloader` (their files lived under the old `/boot` path,
   so the purge does not touch `/boot/firmware`; check with checksums anyway),
   make `tryboot.txt` the `config.txt` and reboot normally.
