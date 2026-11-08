# Frigate NVR

Frigate records and runs object detection for the two door cameras. Home
Assistant uses its person-occupancy sensors to turn on the porch and rear
floodlights.

| | |
|---|---|
| Service | `frigate` in `docker/docker-compose.yml` (`ghcr.io/blakeblackshear/frigate:<version>-tensorrt`, pinned by digest) |
| Config | `docker/frigate/config.yml` → `/persistent_data/application/frigate/config.yml` |
| Database, model | `/persistent_data/application/frigate/` (`frigate.db`, `model_cache/`) |
| Recordings | ZFS dataset `mpool/frigate` at `/mpool/frigate` (`quota=1T`, `com.sun:auto-snapshot=false`) |
| Network | `internal`, fixed address `frigate_ip` (10.3.32.12) |
| UI | https://frigate.lab.nkontur.com (lab_nginx; LAN and tailnet only, Frigate login) |
| Secrets (Vault) | `cameras/rear`, `cameras/doorbell`, `docker/frigate` (`mqtt_password`, `admin_password`, `ha_password`) |

## Cameras

| Camera | Device | Address | Record | Detect |
|---|---|---|---|---|
| `back_door` | Amcrest (inventory `back_camera_address`) | 10.6.128.14 | main, 2592x1944 H.264 20 fps | sub, 704x480 at 5 fps |
| `front_door` | Amcrest AD410 doorbell (`doorbell_address`) | 10.6.128.9 | main | sub, scaled to 640x480 at 5 fps |

While the doorbell is offline, Frigate restarts its ffmpeg processes every few
seconds (~230 log lines a minute). `logger.logs` in config.yml lowers those
loggers to `critical` and the go2rtc `rtsp` module to `error`; delete the
overrides once the doorbell is back.

go2rtc holds one connection per camera stream. Detect, record and the live view
read from it on loopback (`rtsp://127.0.0.1:8554/<stream>`). The go2rtc API
(1984), its RTSP restream (8554) and WebRTC (8555) listen on loopback only:
they have no authentication. The restream is therefore not available to Home
Assistant, which shows Frigate's snapshots and clips instead.

## Ports and access

- **8971**: authenticated UI and API (TLS off; lab_nginx terminates TLS). This
  is the only port reachable from outside the container. Home Assistant logs in
  here as the Frigate user `homeassistant`.
- **5000**: unauthenticated (admin rights). Bound to `127.0.0.1` in the
  container (`networking.listen.internal`). Used by the healthcheck and the
  deploy, which manage users through `docker exec`.
- Firewall (`networking/iptables/rules.v4`): `frigate_ip` may open TCP 80 and
  554 to the two cameras and TCP 8883 to mosquitto. Nothing else on the
  internal VLAN reaches the cameras.

Users: the deploy (`configure-homelab-services`, `tasks/frigate_post.yml`)
sets the `admin` password to Vault's `admin_password` when that one does not
log in, and creates or fixes `homeassistant` (role `frigate_ha_role`) with
`ha_password`. Other users made in the UI are left alone.

## Detection on the P5000

The `-tensorrt` image runs the `onnx` detector through ONNX Runtime with the
CUDA/TensorRT execution providers. Frigate supports Nvidia GPUs with compute
capability 5.0 or later and a driver 545 or later, with CUDA 12 libraries.
The Quadro P5000 is Pascal (6.1) and the host driver is 580, so it is
supported. Frigate's own figure for a GTX 1070 (also Pascal) is about 16 ms
for YOLOv9-s at 320x320. Decoding uses NVDEC (`preset-nvidia`).

Model: YOLOv9-s 320x320 (`model_type: yolo-generic`), Frigate's recommended
family. It is not in the image. `docker/frigate/build-yolov9-model.sh` exports
it with Frigate's documented recipe, pinned to a yolov9 commit, in a CPU-only
build container. The deploy runs the script only when
`model_cache/yolov9-s-320.onnx` is missing. To change the model size, build
the new file with the script (`s|t|m ...`) and change `model.path`.

The GPU is shared with Plex (NVDEC/NVENC transcodes) and Ollama. Frigate needs
well under 1 GB of the 16 GB VRAM. Check `nvidia-smi` after changes: the
`ffmpeg` processes of Frigate and Plex's transcoder should both be listed.

## Retention and disk use

The math is in the comments of `docker/frigate/config.yml`. In short:

- Bitrate measured 2026-11-07 with a 60 s stream copy (`ffprobe` reports no
  bitrate for these cameras): back_door main 6.15 Mbit/s (66 GB/day). The
  doorbell was offline; it is assumed to be the same.
- Continuous recordings 2 days, motion 7 days, alerts 30 days, detections 14
  days (both `mode: motion`), snapshots 30 days.
- Worst case for both cameras (40% motion, 8% tracked objects): about 790 GB.
  With one camera, about 400 GB.

Frigate also deletes the oldest recordings on its own when the filesystem has
less than an hour of recording space left, and the 1T quota is the filesystem
size it sees. To measure the real motion share, see *System > Storage* in the
UI, or `zfs list mpool/frigate` after a week.

## Upgrades

Renovate proposes image updates (renovate.json). Patch releases and digest
refreshes go through the auto-merge sweep with the container healthcheck as
the gate. A 0.x minor release (0.18 → 0.19) migrates `frigate.db` one way and
usually the config format; the auto-merge policy treats it as a major and
leaves it for review. Before merging one:

1. Read the release notes for breaking config changes.
2. Set `version:` in `docker/frigate/config.yml` to the new image's
   `CURRENT_CONFIG_VERSION` (`frigate/util/config.py`). If it lags, Frigate
   migrates the file on start and the next deploy overwrites the migration.
3. Copy `frigate.db` (the deploy does not back it up first).
4. Check that the `-tensorrt` image still ships CUDA 12 libraries. CUDA 13
   dropped Pascal, the same issue as Ollama (docs/ollama.md).

## Home Assistant

The Frigate integration (`blakeblackshear/frigate-hass-integration`) is
installed through the Marketplace (Settings → Marketplace). It is configured
against `http://frigate:8971` with the `homeassistant` user. It creates
`binary_sensor.<camera>_person_occupancy`, used by the floodlight and porch
light automations in the homeassistant repo.

## History

Frigate ran here until `d93eb06c` ("feat: nuke frigate", 2025-12-16) on the
IoT VLAN with the old `tensorrt` detector (YOLOv7 `.trt`). The last config on
disk had already been migrated to 0.16, which removed the `tensorrt` detector
for amd64. Recordings had become sparse (177 MB over the last weeks), which
suggests detection or recording was failing at the end. The old data was
copied, not moved: `/mpool/plex/frigate` → `/mpool/frigate/legacy-2025/`. The
old database is in `/persistent_data/application/frigate/pre-0.18/`.
