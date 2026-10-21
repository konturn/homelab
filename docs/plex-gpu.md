# Plex hardware transcoding (Quadro P5000)

Plex transcodes on the router's NVIDIA Quadro P5000 (Pascal, 16 GB,
NVDEC/NVENC with HEVC 10-bit decode). Quadro cards have no NVENC
concurrent-session cap.

## Code-managed

- **Driver**: installed on the host, not managed by this repo. Driver
  changes and reboots are manual.
- **Container toolkit**: `ansible/roles/configure-docker`, enabled for the
  router by `nvidia_container_runtime: true` in `ansible/router.yml`
  (NVIDIA apt repo + `nvidia-container-toolkit`).
- **Docker runtime**: `docker/daemon.json` registers the `nvidia` runtime.
  A change to that file restarts dockerd (every container), so edit it only
  in a planned window. Docker iptables management stays disabled.
- **Plex service**: `docker/docker-compose.yml` gives Plex the GPU with a
  `deploy.resources.reservations.devices` entry (driver nvidia, count 1,
  capabilities gpu/video/compute/utility). The official `plexinc/pms-docker`
  image needs nothing else.

## Plex preferences (not code-managed)

Plex owns `Preferences.xml` and rewrites it at runtime, so the repo does not
template it. The relevant prefs, set through the Plex API
(`PUT /:/prefs?<name>=<value>`) or Settings > Transcoder:

| pref | value | note |
|---|---|---|
| `HardwareAcceleratedCodecs` | 1 | "Use hardware acceleration when available" (default on) |
| `HardwareAcceleratedEncoders` | 1 | "Use hardware-accelerated video encoding" (default on) |
| `HardwareDevicePath` | the P5000 entry | default "Auto"; the list only shows GPUs visible in the container |
| `TranscoderToneMapping` | 1 | HDR tone mapping (default on) |

Hardware transcoding requires Plex Pass on the server owner account.

## Transcoder temp dir

`TranscoderTempDirectory=/transcode`, bind-mounted from
`/mpool/plex/transcode`. A tmpfs would be faster, but tmpfs pages count
against the container's 8G memory limit, which is sized for scan and
transcode peaks.

## Sharing the GPU

Each Plex transcode uses roughly 200-500 MB of VRAM. Another container (for
example an LLM server) can share the card with its own device reservation;
leave a few GB of VRAM free for Plex.

## Checking it works

```sh
nvidia-smi                      # "Plex Transcoder" in the process list
nvidia-smi dmon -s u            # enc/dec utilisation while transcoding
```

The Plex dashboard (or `/status/sessions`) shows "(hw)" on the transcode.
