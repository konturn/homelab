# Amcrest cameras

The settings that matter on the Amcrest (Dahua-based) cameras are kept as
code in `networking/amcrest/`:

| File | Purpose |
|---|---|
| `back-camera.yaml` | Desired state of the back camera (Frigate's `back_door`, 10.6.128.14, IoT VLAN) |
| `amcrestctl.py` | `export`, `drift`, `check`, `plan` (Python stdlib only) |
| `test_amcrestctl.py` | Offline unit tests |

The tool is host-agnostic: each camera is one YAML file with its `host`,
`username` and the Vault path of its password. Only the back camera has a file.
The front doorbell (AD410, 10.6.128.9) is offline. When it is back, it gets its
own file through `export` (see below).

Everything is read-only. The tool sends only `configManager.cgi?action=getConfig`
and `magicBox.cgi?action=get*` requests. `plan` prints the `setConfig` requests
that would restore the file, and nothing sends them: there is no `apply`.

## What is captured

`amcrestctl.MANAGED` lists the settings. Values are kept as the camera reports
them (`table.All.` stripped), grouped:

| Group | Settings | Desired value on the back camera |
|---|---|---|
| `time` | `NTP.*` (enable, server, port, time zone, update period), `Locales.DST*` (DST on, by week, start and end) | NTP from the router (10.6.0.1, the IoT VLAN gateway; the camera has no internet). Time zone index 25 (GMT-05:00; the description string the camera reports is `CentralAmerica`), US DST rule: 2nd Sunday of March to 1st Sunday of November, 02:00 |
| `cloud` | `T2UServer.Enable` (P2P for the Amcrest View app), `DDNS[*].Enable`, `UPnP.Enable`, app push, email, health report email, cloud firmware auto-check | P2P, DDNS, UPnP off |
| `outbound` | Places the camera would push to: auto-register server, FTP (`NAS[*]`), SFTP, RTMP, Amcrest VSP, alarm server, syslog | All off, addresses cleared to `0.0.0.0` (one of them held the factory `192.168.1.100` until it was cleared by hand) |
| `network` | `Network.eth0.DhcpEnable`, `IPv6.Enable` | DHCP (the address is the DHCP reservation, `back_camera_address`) |
| `services` | Web/RTSP/DVRIP ports, HTTPS, Telnet, SSH, Bonjour, device discovery, digest auth on HTTP/RTSP/ONVIF, password reset, remote debug | As reported |

`device` (model and firmware) is information only and is not drift-checked.

### Not captured

- Secrets: the admin password lives in Vault (`homelab/cameras/rear`,
  `password`), read at run time. Nothing that holds a password or key is in
  `MANAGED`.
- Serial number, P2P UUID and MAC address (the repo is mirrored publicly).
  `MachineName` and `Bonjour.Name` equal the serial number, so they are left
  out too.
- Image, encoding, motion detection, privacy masks and recording settings.
  Frigate does the detection and recording; these are not security-relevant.
- The address and DNS servers: they come from DHCP.

The internet block is in the firewall, not on the camera: the back camera is in
the `iot_no_wan` ipset and the IoT VLAN gets DNS only from iot-dns
(docs/FIREWALL.md). The `cloud` group keeps the camera itself from trying.

## Usage

Run on the router (or anywhere that reaches the IoT VLAN). With a Vault token in
the environment, or `AMCREST_PASSWORD` set:

```sh
cd networking/amcrest
export VAULT_ADDR=https://vault.lab.nkontur.com:8200 VAULT_TOKEN=$(glab variable get VAULT_TOKEN)
python3 amcrestctl.py export             # rewrite back-camera.yaml from the camera
python3 amcrestctl.py export -o -        # print instead of writing
python3 amcrestctl.py drift              # exit 2 on drift
python3 amcrestctl.py drift --all        # every *.yaml here (what CI runs)
python3 amcrestctl.py check --all        # offline: the files parse and hold only managed keys
python3 amcrestctl.py plan               # dry run: the setConfig requests that would restore the file
```

`--file` picks another camera's file, and `--host` (or `AMCREST_HOST`) overrides
its address. `AMCREST_USER` overrides the file's `username`.

A new camera: write a file with `schema: "1"`, `name`, `host`, `username` and
`vault_path`, then run `amcrestctl.py --file <name>.yaml export` and commit.
After an intended change on a camera, run `export`, review `git diff`, and
commit.

### Login lockout

The camera locks the account after 5 failed logins
(`General.LockLoginTimes`), for 5 minutes. `amcrestctl` makes one digest-auth
attempt per request and stops at the first rejected login. It never retries,
and it never tries another user or password.

## Drift detection

`amcrest:drift` in `.gitlab-ci.yml` runs on the pipeline schedule
**"iot-devices: daily config drift"** (`SCHEDULED_TASK=iot-device-drift`, daily
at 06:32 America/New_York). It runs on the protected runner, whose job network
(`gitlab-ci-protected`, docs/FIREWALL.md) allows the camera's HTTP port, and
reads the password from Vault with the job's JWT (role `ci-deploy`). It is
strictly read-only.

| Result | Exit | Effect |
|---|---|---|
| No differences | 0 | Job passes |
| `DRIFT` (value changed) or `NEW` (managed setting reported but not in the file) | 2 | Job fails and mails the report to root (`Homelab: Amcrest camera settings drift`), the same path as the Denon, Aruba and NETGEAR drift checks |
| Camera unreachable, login rejected, other error | 1 | Same mail |

`UNREAD` lines (a value in the file that the camera did not report) are shown
but do not fail the job. The report is kept as the `amcrest-drift.txt` job
artifact for 30 days.

`amcrest:test` (MRs and main, on changes in `networking/amcrest/`) runs the unit
tests and `amcrestctl.py check --all`.

## Restoring a setting

`plan` prints one `setConfig` request per difference, for example
`/cgi-bin/configManager.cgi?action=setConfig&NTP.Address=10.6.0.1`. Send it by
hand with digest auth after reviewing it:

```sh
curl --digest -u admin "http://10.6.128.14/cgi-bin/configManager.cgi?action=setConfig&NTP.Address=10.6.0.1"
```

The camera answers `OK`. Run `drift` again afterwards.
