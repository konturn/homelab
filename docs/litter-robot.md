# Litter-Robot 4: local control (whiskerless)

The two Litter-Robot 4s can run without Whisker's cloud. Each one is
re-pointed, over Bluetooth, from AWS IoT to our own MQTT broker
(**lr4-broker**). The Home Assistant integration **whiskerless** then reads
and controls the robot locally. No firmware is changed, and the robot's
safety logic stays on its main board. Cycle and Reset are sent as ordinary
panel presses.

**Status:** both robots are still on the cloud (core `litterrobot`
integration). Everything on our side is deployed and idle. A robot moves only
when someone provisions it at the robot (below). Do it **one robot at a time**:
Movie Room first, then about **two weeks of burn-in** before Main.

Device details (serials, firmware, addresses per robot) live in the
confidential issue, not here.

## How it fits together

```
robot ──MQTT/TLS 8883, client cert──▶ lr4-broker (lr4_broker_address, iot)
                                           │ bridge, TLS 8883, user lr4-bridge
                                           ▼
Home Assistant ◀── MQTT ──────────── mosquitto (mosquitto_address)
  whiskerless integration (MQTT discovery on prod/LR4/+/state)
telegraf (router) ── user lr4-monitor ──▶ mosquitto  →  lr4_mqtt  →  alert lr4-mqtt-silent
```

| Piece | Where | Notes |
|---|---|---|
| lr4-broker | `docker/lr4-broker/`, compose service `lr4-broker` | mosquitto (same image and pin as `mosquitto`), 8883 only, `require_certificate`, the certificate CN is the username, no persistence. Runs as uid 1883 with no capabilities, read-only. |
| ACL | `docker/lr4-broker/acl` | Each robot (CN = its serial) gets only `prod/LR4/<serial>/#`. `whiskerless-cli` gets `prod/LR4/#`. |
| Bridge | `docker/lr4-broker/mosquitto.conf` | `state` and `activity` go out, `command` comes in. Clean session, QoS 0, so a command is never replayed late. The bridge's state is retained on mosquitto at `$SYS/broker/connection/lr4-bridge/state`. |
| PKI | Vault | See [Vault](#vault). A private CA (RSA 2048, 10 years) generated with whiskerless's own `pki` module. The server certificate names `lr4_broker_address` as an IP and DNS SAN. One client certificate per robot (CN = serial), and one for the CLI. |
| HA integration | pinned fork `root/ha-whiskerless` (`homeassistant_pinned_integrations`) | Upstream `v0.2.0-rc.76`. It already contains the HA 2026.11 restore-cache fix (upstream issue 13). It is installed, with no config entry until a robot is provisioned. Its `whiskerless==0.2.0-rc.76` requirement is installed from PyPI by HA at the first setup. |
| Laptop CLI | `scripts/litter-robot/whiskerless-mac`, staged on the router in `/usr/local/share/lr4/` | The pinned CLI (hash-locked `requirements-macos.txt`), `--auth supplied`. The CA key never leaves Vault. |
| Router helper | `scripts/litter-robot/lr4ctl` → `/usr/local/sbin/lr4ctl` | `bundle` (certificates from Vault for the laptop, over SSH) and `status`. |
| Monitoring | `telegraf.conf` (lr4_mqtt), Grafana `lr4-mqtt-silent` | See [Monitoring](#monitoring). |

**Network:** the robots have DHCP reservations
(`litter_robot_{1,2}_address`). Robots, lr4-broker and mosquitto are all on VLAN 6, at L2, so no
firewall rule is involved (docs/FIREWALL.md). The robot is provisioned with
the broker's **address**, not a name, so iot-dns does not matter. The robots
keep their WAN access until [Later: cutting WAN](#later-cutting-wan).

**Firmware:** keep each robot on the ESP build it was validated on (recorded
in the confidential issue). Do not install a pending ESP update before or
after going local. A local robot cannot receive Whisker OTA updates anyway.
Getting one would mean re-onboarding to the cloud, updating, and
provisioning again, and a newer build may change how provisioning works.

## Before going to the robot

1. **Laptop:** a Mac with Bluetooth (any Apple Silicon MacBook), on the home
   network (it uses `ssh root@10.4.0.1`). Bluetooth on. The first scan asks to
   let the terminal app use Bluetooth: allow it. If it was denied earlier,
   allow it in *System Settings → Privacy & Security → Bluetooth*.
2. **Install the CLI (once):**
   ```sh
   mkdir -p ~/lr4 && scp 'root@10.4.0.1:/usr/local/share/lr4/*' ~/lr4/
   ~/lr4/whiskerless-mac install      # prints: installed: whiskerless 0.2.0-rc.76 ...
   ```
3. **Set up the store** (CA and the laptop's identity from Vault). This is safe
   to repeat:
   ```sh
   ~/lr4/whiskerless-mac setup
   ```
4. **Check the broker side:**
   ```sh
   ~/lr4/whiskerless-mac status
   ```
   You want `lr4-broker: running healthy` and
   `bridge into mosquitto: connected`. *robots publishing* is `none` before
   the first provision. **Do not continue if either line is wrong:** once
   pairing mode starts, the robot is offline until a provision completes.
5. Have the **IoT Wi-Fi passphrase** ready (the robot joins the IoT SSID,
   2.4 GHz). Also have a **phone with the Whisker app**, in case you need to
   roll back.
6. Do it when the cats can use the other box. The robot can't cycle
   remotely while it is in pairing mode.

## At the robot (about 10 minutes)

Stay within a few metres of the robot. The robot can't be identified
remotely, so check its label: the serial is the unhyphenated `LR4C…` line,
not the `LR4-…-US` model number. The script fills in the serial from Vault
for the robot you name and prints it. **Compare it with the label.**

1. Start a **dry run** (it writes nothing):
   ```sh
   ~/lr4/whiskerless-mac provision movie-room --dry-run
   ```
   It prints `Hold Connect on the robot now…` and starts scanning.
2. **Hold** the robot's **Connect** button for about **3 seconds**, until the
   light **blinks yellow**. Hold it, don't tap it: a short tap turns the
   robot's Wi-Fi off (the light goes white), and another short tap turns it
   back on. From the moment it blinks yellow, the robot has forgotten its
   Wi-Fi and stays off the network until a provision completes. **Don't
   walk away now.**
3. The dry run connects over Bluetooth, then lists the networks **the robot**
   can see. Pick the IoT SSID and type the passphrase. Check the summary
   screen:
   - `serial`: matches the label
   - `broker`: `lr4_broker_address` (10.6.32.11)
   - `wifi`: the IoT SSID
   - `identity`: `supplied by you, CN=<the same serial>`

   Type `yes`. In a dry run this only reads from the robot.
4. With the light **still blinking yellow**, run the real provision:
   ```sh
   ~/lr4/whiskerless-mac provision movie-room
   ```
   Pick the same network, enter the passphrase, check the same summary, and
   type `yes`. The steps should end with `WiFi connected (ip=…)`,
   `CERT_AWS_ROOT_CERT written`, `CERT_DEVICE_CERT written`,
   `CERT_DEVICE_KEY written`, `APPLY_CONFIG committed`, `DEVICE_REBOOT`, and
   then `reprovisioned; the robot should reconnect MQTT to 10.6.32.11`. The
   robot reboots.
5. **Check that it reached the broker** (give it a minute or two):
   ```sh
   ~/lr4/whiskerless-mac status
   ```
   - The robot's serial should appear under *robots publishing*, with a
     non-zero count.
   - The log should show `New client connected from <robot address> … u'<serial>'`.
   - Directly from the laptop:
     ```sh
     ~/lr4/whiskerless-mac cli state
     ```
     This should print the robot's full decoded state.

   If nothing shows up after 5 minutes, see [Troubleshooting](#troubleshooting).
6. **Home Assistant:** *Settings → Devices & services → Discovered*:
   "Whiskerless — Litter-Robot 4 (LR4C…)" → **Add**. Name it *Movie Room
   Litter-Robot* so its entity IDs don't collide with the cloud ones. Leave
   *Empty cycle*, *Power* and *WiFi* disabled (they ship disabled and
   are marked danger).
7. Optional, with fresh litter: press **Calibrate full** on the device page
   (or run `~/lr4/whiskerless-mac cli calibrate full`), so the litter level %
   means something.
8. In the cloud *Litter-Robot* integration, delete **only this robot's**
   device; keep the entry for the other robot. If a "Discovered — Whisker"
   card appears later (after the cloud entry is gone), click **Ignore**.

Then do the same for Main (`provision main`) after the burn-in.

## Burn-in (about 2 weeks, before the second robot)

Watch the moved robot in HA while the other stays on the cloud:

- clean cycles after visits, and cat-detect pauses releasing
- sleep schedule start/end
- drawer full / litter level alerts
- MQTT dropouts: the `lr4-mqtt-silent` alert, and reconnects in
  `docker logs lr4-broker`

Optional: import the upstream notification blueprint into the private
`root/homeassistant` repo to replace the Whisker app's push notifications.

## Rollback (either robot, any time)

1. At the robot, **hold Connect** until the light **blinks yellow**.
2. In the **Whisker app**, choose *Set up / reconnect* (Update Network) for
   that robot and finish onboarding. The app writes Whisker's CA and
   endpoints back, and issues the robot a fresh device certificate.
3. In HA, re-add or reload the cloud *Litter-Robot* integration, and delete
   the robot's whiskerless device.
4. The `lr4-mqtt-silent` alert fires for that serial for up to 7 days. Silence
   it in Grafana by `serial`.
5. If WAN was already cut for it, take it out of `iot_no_wan` first.

## Monitoring

- **`lr4-mqtt-silent`** (Grafana, *General Alerting / litter-robot-alerts*,
  severity warning): a robot that has published through lr4-broker has sent
  nothing for **more than 2 hours**. Data: telegraf subscribes to
  `prod/LR4/+/state|activity` on mosquitto as `lr4-monitor`, and counts
  messages per serial in 5-minute buckets (`lr4_mqtt.messages_count`).
  Robots that were never local have no series, so they never alert. The alert
  goes to the **event router** like every alert, and is not in any email
  class.
- **The usual cause** is that the robot's MQTT client wedges. The broker drops
  it on keepalive timeout, the robot's next TLS handshakes abort, and then it
  stops trying, while it still answers ping and keeps cycling normally. One
  wedge recovered by itself after about 90 minutes, hence the 2 hours.
  **Fix: power-cycle the robot** (unplug it for about 10 s). In
  `docker logs lr4-broker`, the pattern is `Client <serial> has exceeded
  timeout`, then `OpenSSL Error … unexpected eof`, then nothing.
- **Every local robot silent at once** points at the path instead: run
  `lr4ctl status` on the router (lr4-broker health and the bridge state),
  check the `lr4-bridge` and `lr4-monitor` lines in mosquitto's
  `mosquitto.passwd`, and check telegraf.
- `lr4-broker` has a healthcheck (its 8883 listening socket), so the
  container alerts cover the container itself.

## Troubleshooting

| Symptom | Check |
|---|---|
| `no LR4 found advertising` | Hold Connect (don't tap) until it blinks yellow, then rerun. If it is already blinking yellow, just rerun and finish. |
| `could not fetch … from the router` | The laptop must reach `ssh root@10.4.0.1`, and `lr4ctl` needs GitLab and Vault up on the router. |
| Provision OK, but the robot never shows in `status` | `docker logs lr4-broker` on the router. A connection from the robot's address with a TLS error means the certificate or broker host is wrong (it must be the address above). No connection at all means Wi-Fi (wrong SSID or passphrase): hold Connect and provision again. |
| HA shows no *Discovered* card | `lr4ctl status` must list the robot. The card appears after its next `state` message: use the robot, or wait a few minutes. |

## Later: cutting WAN

Optional, and only after both robots have burned in locally. First check in
conntrack how a local robot keeps time (NTP to 10.6.0.1 is fine). Then put
one robot in `iot_no_wan` for a few days, and confirm the sleep schedule still
fires on time. Then add both (docs/FIREWALL.md, "IoT devices without
internet"), and move them out of the "must keep the cloud" list there.

## Vault

| Path (mount `homelab`) | Fields | Read by |
|---|---|---|
| `pki/whiskerless` | `ca_crt`, `ca_key` | nobody automatically. The signing key: only needed to issue a new certificate. |
| `pki/whiskerless/robots/<main\|movie-room>` | `serial`, `crt`, `key` | `lr4ctl bundle <robot>` |
| `pki/whiskerless/cli` | `crt`, `key` (CN `whiskerless-cli`) | `lr4ctl bundle cli` |
| `docker/lr4-broker` | `ca_crt`, `server_crt`, `server_key`, `bridge_password`, `monitor_password` | the deploy (fetch-vault-secrets), `lr4ctl` |
| `gitlab/ha-whiskerless` | deploy token | the deploy (pinned integration) |

**Never regenerate the CA or the server certificate casually.** The robots
trust this CA and verify the server certificate against the provisioned
address. A new CA, or a new `lr4_broker_address`, means provisioning every
robot again at the robot. All of them are valid until 2036. To issue a new
certificate from the same CA, use `whiskerless.pki` with the CA pair from
Vault; this is how they were made.

## Bumping the integration

Review the upstream diff, rebase the fork's `homelab` branch onto the new
tag, tag it `<tag>-homelab.N`, and change `sha` in
`homeassistant_pinned_integrations`. When the CLI version changes, also
update `WANT_VERSION` in `whiskerless-mac` and regenerate
`requirements-macos.txt`:

```sh
uv pip compile <(echo 'whiskerless[ble]==<version>') --python-platform aarch64-apple-darwin \
  --python-version 3.14 --generate-hashes --no-header --no-annotate -o scripts/litter-robot/requirements-macos.txt
```
