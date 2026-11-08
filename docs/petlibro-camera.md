# PETLIBRO feeder camera

The cat feeder is a PETLIBRO PLAF203 (Granary Smart Camera Feeder) on the IoT
VLAN, managed by the PETLIBRO phone app. Its camera reaches Frigate as
`cat_feeder` through the `petlibro-camera` container. **Camera only:** the
feeder itself is not integrated or redirected.

| | |
|---|---|
| Service | `petlibro-camera` in `docker/docker-compose.yml`, image built from `docker/images/petlibro-camera` (CI `petlibro-camera:build`) |
| Software | go2rtc with the `petlibro://` source, source release `v1.9.14-petlibro.1` (checksum-pinned in the Dockerfile) |
| Reviewed fork | private GitLab `root/petlibro-local`, tag `v0.3.14-homelab.1` (`HOMELAB.md`); read-only deploy token in Vault `gitlab/petlibro-local` |
| Networks | `iot` (`petlibro_camera_iot_ip`, 10.6.32.9) to reach the camera; `internal` (`petlibro_camera_ip`, 10.3.32.14) for RTSP |
| RTSP | `rtsp://frigate:<password>@10.3.32.14:8554/cat_feeder`, internal address only |
| Secrets (Vault) | `docker/petlibro-camera`: `camera_uid` (the camera's TUTK UID), `rtsp_password` (also given to Frigate) |
| Stream | 640x360 H.264, about 15 fps, 0.24 Mbit/s, no audio |

## How it works

The feeder speaks two protocols, both to the PETLIBRO cloud:

- **Control:** plaintext MQTT on TCP 1883 to `mqtt.us.petlibro.com`. The
  feeder resolves the name through Pi-hole. The schedule lives on the
  feeder; the cloud is its clock (NTP over MQTT) and the app's control path.
- **Camera:** TUTK/Kalay P2P. The feeder registers with TUTK relay servers on
  UDP 10001 and also answers LAN P2P sessions directly.

go2rtc's `petlibro://` source is a pure-Go TUTK LAN client: no vendor
libraries, no cloud account. It finds the camera on 10.6.0.0/16 by its UID
(LAN search, so a new DHCP address does not matter), opens a viewing session
when someone reads the stream, and closes it when the last reader leaves.
Frigate reads it all the time, so the session stays open.

go2rtc is locked down in `docker/images/petlibro-camera/go2rtc.yaml`:

- `app.modules` loads only `rtsp` and `petlibro`. The HTTP API (config
  writes, restart), WebRTC and the exec/ffmpeg/echo/expr sources are never
  initialised. The upstream add-on's API listens on all interfaces without
  authentication and with `origin: "*"`, which allows config injection and
  code execution, so it is not used.
- One fixed stream. RTSP needs Frigate's login and listens on the internal
  address only.
- The container runs unprivileged and read-only, with no capabilities. Its
  only traffic is UDP to the camera and RTSP to Frigate.

## What is deliberately NOT done

The upstream project (`tannerln7/Petlibro-Home-Assistant`) is a full local
backend. Its MQTT controller replaces the PETLIBRO cloud, and its installer
pushes an OTA that adds an agent and root SSH to the feeder. Neither is used,
and neither should be added without a decision:

- The firmware treats an empty feeding-plan reply as authoritative and wipes
  the schedule. A local backend answers those requests and pushes the time.
- The feeder's clock comes over MQTT. With the cloud cut off and the local
  backend down, a rebooted feeder would have no time and could miss meals.
- The phone app stops working while the feeder is on a local broker.

So there is no DNS override, no firewall rule and no MQTT change for the
feeder. Feeding does not depend on this container.

## Security note

The camera's LAN login is hardcoded in the feeder firmware and cannot be
changed. **Anything on the IoT VLAN that knows (or sniffs) the UID can view
the camera.** The UID travels, lightly obfuscated, in the feeder's TUTK
traffic. This is a property of the device, not of this setup. Keep it in mind
for what the camera points at. The relay itself adds no exposure on the IoT
VLAN: RTSP is only on the internal address, with a password.

## Operations

- **Health:** the container healthcheck covers the RTSP listener only.
  Camera trouble shows in Frigate (`cat_feeder` at 0 fps) and in the
  container log (`petlibro physical viewing session ...`).
- **UID:** it never changes for a device. For a replacement feeder, read it
  in the PETLIBRO app (camera details) or on the label, and update Vault
  `docker/petlibro-camera` `camera_uid`.
- **Log warning** `SO_RCVBUF clamped`: harmless. The host's
  `net.core.rmem_max` caps the UDP receive buffer; the stream is small.
- **Bumps:** manual. Review the new go2rtc source release (diff it against
  upstream AlexxIT/go2rtc), update the fork and its `-homelab.N` tag, then
  change the version, URL and SHA-256 in the Dockerfile. Renovate does not
  track it.

## Revert

1. Remove the `cat_feeder` stream, camera and comment from
   `docker/frigate/config.yml`, and `FRIGATE_PETLIBRO_RTSP_PASSWORD` from the
   frigate service.
2. Remove the `petlibro-camera` service, the CI jobs and needs, the inventory
   addresses, the Vault mapping and the dependency node. Merge.
3. The router deploy removes the orphaned container (`remove_orphans`).
   For an immediate stop: `docker rm -f petlibro-camera`.
4. Optionally delete Vault `docker/petlibro-camera` and `gitlab/petlibro-local`,
   and the GitLab project `root/petlibro-local`.

Nothing on the feeder or its cloud link changes, so there is nothing to undo
there.
