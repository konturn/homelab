# Network Architecture

This document describes the homelab network topology, VLAN structure, and traffic routing.

## Network Diagram

```
                                    ┌─────────────────────┐
                                    │      INTERNET       │
                                    └──────────┬──────────┘
                                               │
                        ┌──────────────────────┼──────────────────────┐
                        │                      │                      │
                        ▼                      ▼                      ▼
              ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
              │   WAN (DHCP)    │    │   Mullvad VPN   │    │   Wireguard     │
              │ enx6c1ff76b2ec9 │    │ us-chi-wg-201   │    │   wg0/wg1       │
              └────────┬────────┘    └────────┬────────┘    └────────┬────────┘
                       │                      │                      │
                       └──────────────────────┼──────────────────────┘
                                              │
                                    ┌─────────▼─────────┐
                                    │   ROUTER HOST     │
                                    │ router.lab.nkontur│
                                    │                   │
                                    │   bond0 (LACP)    │
                                    │ enp4s0f0+enp4s0f1 │
                                    └─────────┬─────────┘
                                              │
           ┌──────────────────────────────────┼──────────────────────────────────┐
           │                                  │                                  │
     ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴─────┐  ┌─────┴─────┐
     │  VLAN 2   │  │  VLAN 3   │  │  VLAN 4   │  │  VLAN 5   │  │  VLAN 6   │  │  VLAN 7   │
     │ External  │  │ Internal  │  │   Mgmt    │  │   Guest   │  │    IoT    │  │ Reserved  │
     │10.2.0.0/16│  │10.3.0.0/16│  │10.4.0.0/16│  │10.5.0.0/16│  │10.6.0.0/16│  │10.7.0.0/16│
     └───────────┘  └───────────┘  └───────────┘  └───────────┘  └───────────┘  └───────────┘
```

## VLAN Breakdown

All VLANs use macvlan Docker networks, allowing containers to have their own IP addresses on the physical network.

### VLAN 2 — External (10.2.0.0/16)

**Purpose:** Internet-facing services accessible from outside the network.

| IP Range | Purpose |
|----------|---------|
| 10.2.0.1 | Gateway (bond0.2) |
| 10.2.0.0/18 | Docker container pool |
| 10.2.32.1 | nginx (reverse proxy) |

**Services:**
- **nginx** (10.2.32.1) — Main reverse proxy for all external traffic
- **Plex** — Media streaming (also on internal for LAN access)
- **Nextcloud** — File sync and sharing
- **Bitwarden** (Vaultwarden) — Password manager
- **Audioserve** — Audiobook streaming
- **WordPress** — Blog
- **Ombi** — Media request management

**Egress:** Direct to WAN (not through Mullvad VPN).

---

### VLAN 3 — Internal (10.3.0.0/16)

**Purpose:** Lab services not exposed to the internet.

| IP Range | Purpose |
|----------|---------|
| 10.3.0.1 | Gateway (bond0.3) |
| 10.3.0.0/18 | Docker container pool |
| 10.3.32.1 | lab_nginx (internal reverse proxy) |
| 10.3.32.2 | PiHole (DNS ad-blocking) |
| 10.3.32.3 | Deluge (torrent client) |
| 10.3.64.0 - 10.3.127.255 | DHCP range |

**Services:**
- **lab_nginx** (10.3.32.1) — Internal reverse proxy
- **PiHole** (10.3.32.2) — DNS server with ad-blocking
- **Deluge** (10.3.32.3) — Torrent client (egresses via Mullvad)
- **GitLab** — Source control and CI/CD
- **Moltbot Gateway** — AI assistant
- **Radarr/Sonarr/Prowlarr** — Media automation
- **InfluxDB/Grafana** — Metrics and dashboards
- **Paperless-ngx** — Document management
- **Home Assistant** (multi-homed, also on IoT + External)

**Egress:** Routes through Mullvad VPN (marked traffic → route table 252).

---

### VLAN 4 — Management (10.4.0.0/16)

**Purpose:** Infrastructure management, switches, IPMI, container registry.

| IP Range | Purpose |
|----------|---------|
| 10.4.0.1 | Gateway (bond0.4) / DHCP server |
| 10.4.0.0/18 | Docker container pool |
| 10.4.32.1 | Container Registry |
| 10.4.64.0 - 10.4.127.255 | DHCP range |
| 10.4.128.x | Static device reservations |

**Devices:**
- **Registry** (10.4.32.1) — Docker container registry
- **Movie Switch** (10.4.128.3) — Managed switch
- **Office Switch** (10.4.128.4) — Managed switch
- **IPMI** (10.4.128.7) — Server management interface
- **UPS** (10.4.128.8) — Network-connected UPS
- **Access Points** (10.4.128.21-23) — WiFi APs

**Access:** Full routing privileges, can reach all other VLANs.

---

### VLAN 5 — Guest (10.5.0.0/16)

**Purpose:** Guest WiFi network with limited access.

| IP Range | Purpose |
|----------|---------|
| 10.5.0.1 | Gateway (bond0.5) |
| 10.5.64.0 - 10.5.127.255 | DHCP range |

**Access:** Can reach external services (nginx) but isolated from internal networks.

---

### VLAN 6 — IoT (10.6.0.0/16)

**Purpose:** IoT devices, home automation, multiroom audio.

| IP Range | Purpose |
|----------|---------|
| 10.6.0.1 | Gateway (bond0.6) |
| 10.6.0.0/18 | Docker container pool |
| 10.6.32.x | Container static IPs |
| 10.6.64.0 - 10.6.127.255 | DHCP range |
| 10.6.128.x | Static device reservations |

**Container Services:**
| IP | Service |
|----|---------|
| 10.6.32.2 | Snapserver (multiroom audio server) |
| 10.6.32.3 | Mosquitto (MQTT broker) |
| 10.6.32.5 | iot_nginx (IoT reverse proxy) |
| 10.6.32.6 | Ambient Weather bridge |
| 10.6.32.7 | Mopidy (music player) |

**Physical Devices:**
| IP | Device |
|----|--------|
| 10.6.128.3 | Denon receiver |
| 10.6.128.4 | Projector |
| 10.6.128.5 | Shield TV |
| 10.6.128.6 | Z-Wave satellite (Raspberry Pi) |
| 10.6.128.9 | Doorbell camera |
| 10.6.128.10 | Kitchen air quality sensor |
| 10.6.128.11 | Satellite-2 (Zigbee coordinator) |
| 10.6.128.14 | Back camera |
| 10.6.128.15 | Bedroom air quality sensor |
| 10.6.128.16 | Weather station |
| 10.6.128.18 | Robot vacuum |
| 10.6.128.19 | Apple TV |
| 10.6.128.20 | Zigbee radio |

**Egress:** Routes through Mullvad VPN (marked traffic).

---

### VLAN 7 — Reserved (10.7.0.0/16)

**Purpose:** Reserved for future use.

| IP Range | Purpose |
|----------|---------|
| 10.7.0.1 | Gateway (bond0.7) |
| 10.7.64.0 - 10.7.127.255 | DHCP range |

**Egress:** Routes through Mullvad VPN (marked traffic).

---

## Service-to-Network Mapping

### Multi-homed Containers

Some containers need access to multiple networks:

| Container | Networks | Reason |
|-----------|----------|--------|
| homeassistant | internal, iot, external | Controls IoT devices, exposes UI externally, uses internal DNS |
| plex | external, internal | External streaming + local media access |
| ombi | external, internal | External requests + internal API calls |

### Network Isolation by Service Type

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           EXTERNAL (VLAN 2)                              │
│  nginx ─── nextcloud ─── bitwarden ─── plex ─── audioserve ─── wordpress│
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ (homeassistant, plex, ombi)
┌────────────────────────────────────┴────────────────────────────────────┐
│                           INTERNAL (VLAN 3)                              │
│  gitlab ─── moltbot ─── radarr ─── sonarr ─── pihole ─── grafana        │
│  deluge ─── influxdb ─── paperless ─── prowlarr ─── lab_nginx           │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │ (homeassistant)
┌────────────────────────────────────┴────────────────────────────────────┐
│                              IoT (VLAN 6)                                │
│  snapserver ─── mosquitto ─── zigbee2mqtt ─── mopidy ─── amcrest2mqtt   │
│  snapclient_* ─── ambientweather ─── iot_nginx                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Traffic Flow

### External Request → Service

```
Internet
    │
    ▼ (port 443, 32400)
┌──────────────────┐
│   WAN Interface  │ ─── DNAT ───┐
│ enx6c1ff76b2ec9  │             │
└──────────────────┘             │
                                 ▼
                    ┌─────────────────────┐
                    │   nginx (10.2.32.1) │
                    │   Reverse Proxy     │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
        ┌──────────┐    ┌──────────┐    ┌──────────┐
        │ Nextcloud│    │   Plex   │    │Bitwarden │
        └──────────┘    └──────────┘    └──────────┘
```

**Port Forwarding Rules (iptables PREROUTING):**
- Port 443 → nginx (10.2.32.1):443
- Port 32400 → nginx (10.2.32.1):32400 (Plex direct)
- Port 25564/25565 → nginx (for Minecraft servers)

### Internal Service → Internet (via Mullvad)

```
┌─────────────────────────────────────────────────────────────┐
│                    INTERNAL/IoT Container                    │
│                    (e.g., Deluge on VLAN 3)                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼ (destination not 10.0.0.0/8)
                    ┌──────────────┐
                    │ iptables     │
                    │ mangle       │ ─── MARK 0x1
                    │ PREROUTING   │
                    └──────┬───────┘
                           │
                           ▼ (policy routing: mark 0x1 → table 252)
                    ┌──────────────────┐
                    │ Route Table 252  │
                    │ (mullvad)        │
                    └────────┬─────────┘
                             │
                             ▼
                    ┌────────────────────┐
                    │ Mullvad WireGuard  │
                    │  us-chi-wg-201     │ ─── MASQUERADE ───► Internet
                    └────────────────────┘
```

**Traffic Marking (mangle table):**
- Traffic from VLAN 3 (internal) destined outside 10.0.0.0/8 → marked
- Traffic from VLAN 6 (iot) destined outside 10.0.0.0/8 → marked
- Traffic from VLAN 7 destined outside 10.0.0.0/8 → marked

**Kill Switch:**
Route table 252 has a `prohibit` route as fallback — if Mullvad is down, traffic is blocked rather than leaking to WAN.

### LAN Device → External Service (Hairpin NAT)

When IoT devices (VLAN 6) access external services via public DNS:

```
┌────────────────────┐
│   IoT Device       │
│   (10.6.x.x)       │
└─────────┬──────────┘
          │ (destination: public IP)
          ▼
   ┌──────────────────┐
   │ ipset: wan_ip    │ ─── matches public WAN IP
   └────────┬─────────┘
            │
            ▼ DNAT to nginx (10.2.32.1)
   ┌────────────────────────────────────┐
   │         nginx (10.2.32.1)          │
   └────────────────────────────────────┘
            │
            ▼ SNAT (source → 10.6.0.1)
   ┌────────────────────────────────────┐
   │    Response returns correctly      │
   └────────────────────────────────────┘
```

---

## Wireguard VPN Setup

Two Wireguard interfaces provide remote access:

### wg0 — Primary VPN (10.0.0.0/24)

```
[Interface]
Address = 10.0.0.1/24
ListenPort = 51871

[Peer] # Device 1
AllowedIPs = 10.0.0.2/32

[Peer] # Device 2
AllowedIPs = 10.0.0.4/32
```

**Access:**
- Can reach internal services (ports 443, 5201, 2049, 2342, 33333)
- Can reach DNS (port 53)
- Can access NFS shares
- Can SSH between VPN peers

### wg1 — Secondary VPN (10.0.1.0/24)

```
[Interface]
Address = 10.0.1.1/24
ListenPort = 51872

[Peer]
AllowedIPs = 10.0.1.2/32
```

**Access:** Full routing privileges (trusted device).

### Remote Access Flow

```
Remote Device
     │
     ▼ (UDP 51871 or 51872)
┌───────────────────┐
│   WAN Interface   │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│   Wireguard       │
│   wg0 / wg1       │
└─────────┬─────────┘
          │
          ▼ (10.0.0.x or 10.0.1.x)
┌───────────────────────────────────┐
│     Internal Services             │
│  GitLab, Grafana, Home Assistant  │
└───────────────────────────────────┘
```

---

## Mullvad VPN Egress

### Purpose

Privacy-sensitive traffic (torrents, general browsing from internal/IoT) exits via Mullvad VPN rather than the home WAN IP.

### Configuration

**Interface:** `us-chi-wg-201` (Chicago endpoint)

**Route Table 252:**
```
# From /etc/iproute2/rt_tables
252     mullvad
```

**Policy Routing (netplan):**
```yaml
bond0:
  routing-policy:
  - from: 0.0.0.0/0
    table: 252
    priority: 30000
    mark: 1        # Traffic marked 0x1 uses table 252
  routes:
  - to: 0.0.0.0/0
    table: 252
    metric: 1
    type: prohibit  # Kill switch: block if VPN down
```

### Which Traffic Uses Mullvad?

| Source | Destination | Egress |
|--------|-------------|--------|
| VLAN 2 (external) | Internet | Direct WAN |
| VLAN 3 (internal) | Internet | Mullvad VPN |
| VLAN 4 (mgmt) | Internet | Direct WAN |
| VLAN 5 (guest) | Internet | Direct WAN |
| VLAN 6 (iot) | Internet | Mullvad VPN |
| VLAN 7 | Internet | Mullvad VPN |

### Port Forwarding via Mullvad

Deluge uses Mullvad's port forwarding for incoming torrent connections:

```
Mullvad VPN
     │ (port 62941)
     ▼ DNAT
┌───────────────────┐
│  Deluge           │
│  (10.3.32.3)      │
└───────────────────┘
```

---

## Physical Network

### Router Host Interfaces

```
┌─────────────────────────────────────────────────────────────┐
│                      Router Host                             │
│                                                              │
│  enx6c1ff76b2ec9  ─── WAN (DHCP from ISP)                   │
│                                                              │
│  enp2s0f1         ─── Direct link (10.100.0.2/24)           │
│                                                              │
│  bond0            ─── LACP bond to managed switches          │
│   ├── enp4s0f0                                               │
│   └── enp4s0f1                                               │
│        │                                                     │
│        ├── bond0.2 (VLAN 2) ─── 10.2.0.1                    │
│        ├── bond0.3 (VLAN 3) ─── 10.3.0.1                    │
│        ├── bond0.4 (VLAN 4) ─── 10.4.0.1                    │
│        ├── bond0.5 (VLAN 5) ─── 10.5.0.1                    │
│        ├── bond0.6 (VLAN 6) ─── 10.6.0.1                    │
│        └── bond0.7 (VLAN 7) ─── 10.7.0.1                    │
└─────────────────────────────────────────────────────────────┘
```

### Macvlan Shim Interfaces

Docker macvlan networks can't communicate with the host by default. Shim interfaces solve this:

```
# Each VLAN has a shim for host ↔ container communication
vlan2-shim ─── Allows router to reach VLAN 2 containers
vlan3-shim ─── Allows router to reach VLAN 3 containers
vlan4-shim ─── Allows router to reach VLAN 4 containers
vlan5-shim ─── Allows router to reach VLAN 5 containers
vlan6-shim ─── Allows router to reach VLAN 6 containers
```

---

## DNS Configuration

### PiHole (10.3.32.2)

Primary DNS for VLAN 4 (mgmt) and VLAN 6 (iot):
- Ad-blocking
- Local DNS entries via `custom.list`
- Custom DHCP/DNS overrides via `custom.conf`

### Fallback DNS

- 1.1.1.1 (Cloudflare)
- 8.8.8.8 (Google)

### Local Domain

All services use `*.lab.nkontur.com` for internal DNS.

---

## DHCP Configuration

ISC DHCP server runs on the router, serving:

| VLAN | Range | DNS |
|------|-------|-----|
| 3 (internal) | 10.3.64.0 - 10.3.127.255 | 1.1.1.1 |
| 4 (mgmt) | 10.4.64.0 - 10.4.127.255 | PiHole, 1.1.1.1 |
| 5 (guest) | 10.5.64.0 - 10.5.127.255 | 1.1.1.1 |
| 6 (iot) | 10.6.64.0 - 10.6.127.255 | PiHole, 1.1.1.1 |
| 7 | 10.7.64.0 - 10.7.127.255 | 1.1.1.1 |

Static reservations are defined for critical devices (see DHCP config).

---

## Satellite Nodes

Raspberry Pi satellites extend the network:

### zwave.lab.nkontur.com (10.6.128.6)
- Z-Wave controller
- Snapclient (movie room audio)
- Local nginx for services

### satellite-2.lab.nkontur.com (10.6.128.11)
- Zigbee coordinator
- Document scanner
- Local nginx

---

## Security Boundaries

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            TRUSTED ZONE                                  │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  VLAN 4 (Management)                                             │    │
│  │  - Full access to all VLANs                                      │    │
│  │  - SSH access everywhere                                         │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  Wireguard VPN (wg0, wg1)                                        │    │
│  │  - Access to internal services                                   │    │
│  │  - wg1 has full routing                                          │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                         SEMI-TRUSTED ZONE                                │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  VLAN 3 (Internal)                                               │    │
│  │  - Lab services, no direct internet exposure                     │    │
│  │  - Egress via Mullvad VPN                                        │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  VLAN 6 (IoT)                                                    │    │
│  │  - Can reach PiHole for DNS                                      │    │
│  │  - Can reach registry for image pulls                            │    │
│  │  - Egress via Mullvad VPN                                        │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                           UNTRUSTED ZONE                                 │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  VLAN 5 (Guest)                                                  │    │
│  │  - Internet access only                                          │    │
│  │  - Can reach external nginx (hairpin NAT)                        │    │
│  │  - Isolated from internal networks                               │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  VLAN 2 (External)                                               │    │
│  │  - Exposed to internet                                           │    │
│  │  - Direct WAN egress (no VPN)                                    │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Quick Reference

### IP Cheat Sheet

| Purpose | IP |
|---------|----|
| External nginx | 10.2.32.1 |
| Internal nginx | 10.3.32.1 |
| PiHole DNS | 10.3.32.2 |
| Deluge | 10.3.32.3 |
| Registry | 10.4.32.1 |
| IoT nginx | 10.6.32.5 |
| Snapserver | 10.6.32.2 |
| MQTT broker | 10.6.32.3 |
| Wireguard gateway | 10.0.0.1 |

### Port Reference

| Port | Protocol | Service |
|------|----------|---------|
| 22 | TCP | SSH |
| 443 | TCP | HTTPS (nginx) |
| 51871 | UDP | Wireguard (wg0) |
| 51872 | UDP | Wireguard (wg1) |
| 1883 | TCP | MQTT (TLS) |
| 32400 | TCP | Plex |
| 8123 | TCP | Home Assistant |
