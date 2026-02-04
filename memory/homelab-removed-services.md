# Homelab Removed Services — Archaeology Report
Generated: 2026-02-04

## Summary

27 services/containers removed over the repo's history (2022–2026). Grouped by category with assessment.

---

## 🎤 Voice Assistant Stack (removed Nov 2025)

| Service | Purpose | Removed |
|---------|---------|---------|
| **Rhasspy** | Voice assistant platform (wake word → STT → intent → TTS) | Jul 2024 |
| **Piper** | Wyoming TTS server (local text-to-speech) | Nov 2025 |
| **Whisper** | Wyoming STT server (local speech-to-text, CUDA-accelerated) | Nov 2025 |
| **OpenWakeWord** | Wyoming wake word detection ("Alexa" model) | Nov 2025 |

**What it was:** A full local voice assistant pipeline integrated with Home Assistant via the Wyoming protocol. Whisper ran with NVIDIA GPU acceleration (CUDA libs mounted). Piper used `en_US-ryan-high` voice. OpenWakeWord listened for "Alexa" trigger.

**Why removed:** Commit message is generic ("remove some containers, upgrade others"). Likely: voice assistant never worked reliably enough to justify the resource usage, or Noah switched to commercial alternatives.

**Worth bringing back?** ⭐ **MAYBE — with new context.** Now that I (Prometheus) exist and have TTS capability, a local STT pipeline could enable voice interaction with me through Home Assistant. The Wyoming protocol stack is mature now. However, this is a significant resource commitment (Whisper alone needs GPU). Would need a clear use case beyond "cool to have." If Noah wants voice commands to the house or voice interaction with me, this is the path.

---

## 📷 Computer Vision Stack (removed Dec 2025)

| Service | Purpose | Removed |
|---------|---------|---------|
| **Frigate** | NVR with real-time object detection (NVIDIA GPU) | Dec 2025 |
| **CompreFace** (5 containers) | Face recognition API (admin, api, core, postgres, UI) | Dec 2025 |
| **Double-Take** | Frigate ↔ CompreFace integration for person identification | Dec 2025 |

**What it was:** Full security camera pipeline. Frigate did object detection on camera feeds using NVIDIA GPU, stored clips to `/mpool/plex/frigate`. CompreFace provided face recognition. Double-Take glued them together — when Frigate detected a person, Double-Take sent the crop to CompreFace for identification. Custom Docker image (`registry.lab.nkontur.com/double-take:test9`) suggests active development.

**Why removed:** "nuke compreface and double-take" / "nuke frigate" — two commits same day (Dec 16, 2025). Likely: too resource-heavy, unstable, or the cameras weren't providing enough value. CompreFace alone was 5 containers including its own Postgres.

**Worth bringing back?** ⭐⭐ **YES — but lighter.** The cameras still exist (doorbell at 10.6.128.9, back camera at 10.6.128.14). Frigate is the gold standard for home NVR. CompreFace was probably overkill — we now have our own face recognition skill that could fill that role with far less overhead. A lighter stack could be: Frigate (for detection/recording) + our face-api.js skill (for identification when needed). Would need to assess GPU availability.

---

## 🎮 Gaming (removed May 2024)

| Service | Purpose | Removed |
|---------|---------|---------|
| **Minecraft** | Minecraft server | May 2024 |
| **Minecraft Danny** | Second Minecraft server (for Danny?) | May 2024 |

**Why removed:** Removed during Cloudflare DNS migration. Likely: servers weren't actively used anymore.

**Worth bringing back?** **No** — unless Noah specifically wants a game server again. Low priority.

---

## 🌐 DNS / Networking (removed 2022–2024)

| Service | Purpose | Removed |
|---------|---------|---------|
| **BIND** | DNS server (had db.lab.nkontur.com zone file) | Sep 2024 |
| **PiHole2** | Secondary PiHole DNS | Jul 2022 |
| **PiHole3** | Tertiary PiHole DNS | Jul 2022 |

**Why removed:** BIND nuked when DNS moved to Cloudflare. PiHole2/3 removed during Ansible restructure — single PiHole was sufficient.

**Worth bringing back?** **No.** Cloudflare DNS + single PiHole is simpler and more reliable.

---

## 🏠 Home / Lifestyle (removed 2022–2023)

| Service | Purpose | Removed |
|---------|---------|---------|
| **Grocy** | Grocery/household inventory management | Nov 2022 |
| **OSRM** | Open Source Routing Machine (self-hosted maps/directions for Ohio) | Jul 2023 |
| **Headway** | Map/routing frontend (paired with OSRM) | Jul 2023 |

**Why removed:** Grocy: probably too much overhead for grocery tracking. OSRM/Headway: "remove some unused infra" — self-hosted routing is cool but Google Maps exists.

**Worth bringing back?** **No** for all three. Grocy requires manual inventory management discipline. OSRM was a novelty.

---

## 🖨️ 3D Printing (removed Nov 2025)

| Service | Purpose | Removed |
|---------|---------|---------|
| **OctoPrint** | 3D printer management interface | Nov 2025 |
| **Obico** | AI-powered print failure detection (separate compose file) | Earlier |

**Why removed:** OctoPrint removed in the same "remove some containers" batch. Obico removed separately ("remove obico for now").

**Worth bringing back?** **Only if Noah still has a 3D printer** and actively uses it. Ask him.

---

## 📊 Monitoring (refactored, not really "removed")

| Service | Purpose | Removed |
|---------|---------|---------|
| **Promtail** | Log shipping agent for Loki | Feb 2026 |
| **GitLab Runner** | CI/CD runner container | Jun 2022 |

**Promtail** was replaced by Docker logging driver → Loki direct. Better architecture, not a loss.

**GitLab Runner** was moved to run differently (not as a docker-compose service). GitLab CI still works.

**Worth bringing back?** **No** — both were architectural improvements, not removals.

---

## 🔊 Snapcast Clients (refactored Nov 2022)

| Service | Purpose | Removed |
|---------|---------|---------|
| **snapclient** (generic) | Generic Snapcast client | Nov 2022 |
| **snapclient_kitchen** (old) | Kitchen audio | Nov 2022 |
| **snapclient_main_bedroom** (old) | Bedroom audio | Nov 2022 |

**Why removed:** Refactored to use udev rules for proper soundcard assignment. Replaced with the current per-room snapclient containers (which still exist: office, kitchen, bedroom, bathrooms, guest rooms, global).

**Worth bringing back?** **No** — already replaced by better implementation.

---

## 📄 Other Removed Files

| File | Purpose |
|------|---------|
| `docker-compose-fallback.yml` | Fallback compose (unknown purpose) |
| `docker-compose-scanner.yml` | Document scanner satellite config |
| `docker-compose-obico.yml` | 3D print AI detection |
| `docker-headway-init.yml` | Headway/OSRM bootstrap |
| `generate-records.py` | DNS record generator (for BIND) |
| `bind/` configs | BIND DNS zone files |
| `frigate/config.yml` | Frigate NVR config |
| `double-take/config/` | Double-Take facial recognition config |

---

## Recommendations

### Bring Back (with modifications):
1. **Frigate** ⭐⭐ — Cameras exist but have no NVR. Frigate + our face recognition skill could be a lighter alternative to the old CompreFace stack. Creates a real security system.

### Consider:
2. **Voice Assistant Stack** ⭐ — Only if there's a concrete use case (e.g., "Hey Prometheus" wake word → STT → me → TTS → response through house speakers). Cool but resource-heavy.

### Don't Bother:
3. Everything else — either replaced by better alternatives, was never fully used, or serves no current need.

### Patterns Noticed:
- **Nov 2025 was a big cleanup** — voice stack, OctoPrint, and others removed in one commit
- **Dec 2025 was a vision cleanup** — Frigate + CompreFace nuked same day
- **Services that require manual engagement** (Grocy, OctoPrint) don't survive
- **Services that run passively** (monitoring, media, automation) tend to stick
- **Resource-heavy stacks** (5-container CompreFace, GPU Frigate) get nuked when they're not actively valued

---

*End of report*
