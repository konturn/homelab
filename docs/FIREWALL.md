# Firewall Architecture

This document describes the iptables firewall configuration for the homelab infrastructure.

**Last Audit:** January 2025  
**Rules File:** `networking/iptables/rules.v4`

---

## Network Topology

```
                    ┌─────────────────┐
                    │    Internet     │
                    └────────┬────────┘
                             │
               {{ inet_interface_name }}
                             │
                    ┌────────┴────────┐
                    │     Router      │
                    │  (iptables)     │
                    └────────┬────────┘
                             │
            ┌────────────────┼────────────────┐
            │          bond0 (trunk)          │
            └────────────────┼────────────────┘
                             │
     ┌───────────┬───────────┼───────────┬───────────┐
     │           │           │           │           │
  bond0.2    bond0.3     bond0.4     bond0.5     bond0.6
     │           │           │           │           │
 ┌───┴───┐  ┌───┴───┐  ┌───┴───┐  ┌───┴───┐  ┌───┴───┐
 │external│  │internal│  │ mgmt  │  │ guest │  │  iot  │
 │10.2.x.x│  │10.3.x.x│  │10.4.x.x│ │10.5.x.x│ │10.6.x.x│
 └────────┘  └────────┘  └────────┘  └────────┘  └────────┘
```

### VLANs

| VLAN | Subnet       | Purpose                          | Docker Network |
|------|--------------|----------------------------------|----------------|
| 2    | 10.2.0.0/16  | Internet-facing services         | `external`     |
| 3    | 10.3.0.0/16  | Internal lab services            | `internal`     |
| 4    | 10.4.0.0/16  | Management/infrastructure        | `mgmt`         |
| 5    | 10.5.0.0/16  | Guest network                    | `guest`        |
| 6    | 10.6.0.0/16  | IoT devices                      | `iot`          |

### Additional Interfaces

| Interface | Purpose |
|-----------|---------|
| `wg0`     | Primary WireGuard VPN (port 51871) |
| `wg1`     | Secondary WireGuard VPN (port 51872) |
| `{{ mullvad_interface_name }}` | Mullvad VPN tunnel (privacy routing) |
| `vlanX-shim` | Docker macvlan shim interfaces |

---

## Traffic Flow Summary

### Inbound (Internet → Services)

| Port | Protocol | Destination | Service |
|------|----------|-------------|---------|
| 443  | TCP      | nginx       | HTTPS (reverse proxy) |
| 32400| TCP      | nginx       | Plex Media Server |
| 25564| TCP      | nginx       | Minecraft (Bedrock/secondary) |
| 25565| TCP      | nginx       | Minecraft (Java) |
| 51871| UDP      | router      | WireGuard VPN |
| 62941| TCP      | deluge (via Mullvad) | BitTorrent |

### Outbound (Internal → Internet)

All local VLANs can reach the internet. Some traffic is routed through Mullvad VPN for privacy.

### Inter-VLAN Traffic

| Source | Destination | Policy |
|--------|-------------|--------|
| mgmt (10.4.x.x) | Any | **ALLOW ALL** (trusted) |
| internal (10.3.x.x) | mgmt | SSH, ICMP only |
| iot (10.6.x.x) | Pi-hole | DNS only |
| iot (10.6.x.x) | registry | Container pulls |
| Any | snapserver | **ALLOW ALL** |
| Shield TV | Any | **ALLOW ALL** |
| Apple TV | Any | **ALLOW ALL** |

---

## Services by Network

### External Network (10.2.x.x)

Internet-facing services proxied through nginx:

| Service | Container | Purpose |
|---------|-----------|---------|
| nginx | `nginx` | Reverse proxy, SSL termination |
| Plex | `plex` | Media streaming |
| Bitwarden | `bitwarden` | Password manager |
| Nextcloud | `nextcloud` | File sync/share |
| WordPress | `wordpress` | Blog |
| Ombi | `ombi` | Plex requests |
| AudioServe | `audioserve` | Audiobook streaming |
| Home Assistant | `homeassistant` | Smart home (also on internal/iot) |

### Internal Network (10.3.x.x)

Lab services (not internet-exposed):

| Service | Container | Purpose |
|---------|-----------|---------|
| GitLab | `gitlab` | Code hosting, CI/CD |
| Grafana | `grafana` | Monitoring dashboards |
| InfluxDB | `influxdb` | Time-series database |
| Pi-hole | `pihole` | DNS ad-blocking |
| Radarr | `radarr` | Movie management |
| Sonarr | `sonarr` | TV management |
| Prowlarr | `prowlarr` | Indexer management |
| Jackett | `jackett` | Torrent indexer (legacy) |
| NZBGet | `nzbget` | Usenet downloader |
| Deluge | `deluge` | BitTorrent client |
| lab_nginx | `lab_nginx` | Internal reverse proxy |
| Paperless-ngx | `paperless-ngx` | Document management |
| iperf3 | `iperf3` | Network testing |
| Draw.io | `diagram` | Diagramming |
| **Moltbot** | `moltbot-gateway` | AI assistant |

### IoT Network (10.6.x.x)

Smart home and media devices:

| Service | Container/IP | Purpose |
|---------|--------------|---------|
| Home Assistant | `homeassistant` | Smart home hub |
| Mosquitto | `mosquitto` | MQTT broker |
| Zigbee2MQTT | `zigbee2mqtt` | Zigbee gateway |
| Snapserver | `snapserver` | Multi-room audio server |
| Snapclients | `snapclient_*` | Audio zone endpoints |
| Mopidy | `mopidy` | Music player |
| iot_nginx | `iot_nginx` | IoT reverse proxy |
| Amcrest2MQTT | `amcrest2mqtt` | Doorbell bridge |
| AmbientWeather | `ambientweather` | Weather station bridge |

### Management Network (10.4.x.x)

Infrastructure services:

| Service | Container | Purpose |
|---------|-----------|---------|
| Registry | `registry` | Docker container registry |

---

## Docker and iptables

Docker's iptables management is **disabled** on the router (`"iptables": false` in `daemon.json`).

**Why:** The router manages its own iptables rules via `netfilter-persistent`. When Docker has
`iptables: true`, it inserts its own chains (`DOCKER-USER`, `DOCKER-FORWARD`, `DOCKER-CT`,
`DOCKER-INTERNAL`, `DOCKER-BRIDGE`) at the top of the FORWARD chain on every restart. This
clobbers custom forwarding rules and can break non-Docker traffic (e.g., guest WiFi on VLAN 5).

**Why it's safe:** All containers in this homelab use macvlan networks — they're placed directly
on the appropriate VLAN interface. Docker's iptables rules (bridge NAT, port mapping, inter-container
isolation) are only relevant for bridge-mode networking, which we don't use. The existing custom
iptables rules already handle all necessary forwarding.

**If Docker networking breaks after a change**, check that the custom iptables rules in
`networking/iptables/rules.v4` cover any new forwarding needs — don't re-enable Docker iptables
management, as it will conflict with the existing ruleset.

---

## Known Issues

### 🔐 Security Recommendations

1. **~~Restrict SSH Input~~** ✅ Partially addressed
   - WAN SSH now has rate limiting (max 4 new connections/60s per source IP)
   - mgmt VLAN and Tailscale access remains unrestricted (trusted networks)
   - Future consideration: remove WAN SSH entirely if Tailscale proves reliable

2. **Device-specific rules**
   - Shield TV and Apple TV have full forward access
   - Consider restricting to specific destination ports/IPs

3. **Audit WireGuard access**
   - wg1 has full forward access (trusted)
   - Verify this is intentional and wg1 is properly secured

---

## Rule Categories

### INPUT Chain

Controls traffic TO the router itself:

| Purpose | Interface | Ports |
|---------|-----------|-------|
| SSH | mgmt, tailscale | 22 |
| SSH (rate-limited) | WAN | 22 |
| ICMP | any | - |
| NFS | mgmt, iot, wg0 | 2049, 111 |
| SMB | mgmt | 139, 445 |
| TFTP | mgmt | 69 |
| WireGuard | any | 51871, 51872 |
| mDNS | !internet | 5353 |

### FORWARD Chain

Controls traffic THROUGH the router:

1. **Established/Related** — Always allowed (stateful)
2. **Trusted networks** — mgmt VLAN, wg1 (full access)
3. **Internet-bound** — All VLANs can reach internet
4. **Inter-VLAN** — Restricted by default
5. **Inbound from internet** — Specific ports to external VLAN
6. **VPN traffic** — WireGuard and Mullvad routing

### NAT Table

1. **DNAT (port forwarding)** — Internet inbound to services
2. **SNAT/MASQUERADE** — Outbound NAT for all networks
3. **Hairpin NAT** — Guest/IoT accessing services via WAN IP

---

## Maintenance

### Adding New Services

1. Determine which network the service should be on
2. If internet-exposed: Add DNAT rule for port forwarding
3. If inter-VLAN access needed: Add FORWARD rule
4. Update this document

### Testing Changes

```bash
# Test before applying
iptables-restore -t < networking/iptables/rules.v4

# Apply via Ansible
ansible-playbook -i inventory deploy.yml --tags iptables
```

### Debugging

```bash
# Watch live traffic
iptables -L -v -n --line-numbers

# Log dropped packets (temporarily)
iptables -A INPUT -j LOG --log-prefix "DROPPED: "
iptables -A FORWARD -j LOG --log-prefix "FWD-DROPPED: "
```

---

## Changelog

| Date | Change |
|------|--------|
| 2022-07-03 | Original rules generated |
| 2025-01 | Audit and documentation added |
| 2026-03 | Document Docker iptables disabled on router |
