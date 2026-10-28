# Firewall Architecture

This document describes the iptables firewall configuration for the homelab infrastructure.

**Last Audit:** June 2026  
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
| `{{ mullvad_interface_name }}` | Mullvad VPN tunnel (privacy routing) |
| `vlanX-shim` | Docker macvlan shim interfaces |

> WireGuard interfaces `wg0` / `wg1` (ports 51871 / 51872) were retired (rules January 2025,
> interfaces and configs October 2026). Remote access is via Tailscale.

---

## Traffic Flow Summary

### Inbound (Internet → Services)

| Port | Protocol | Destination | Service |
|------|----------|-------------|---------|
| 22   | TCP      | router      | SSH (rate-limited: max 4 new conns / 60s per source IP) |
| 443  | TCP      | nginx       | HTTPS (reverse proxy) |
| 32400| TCP      | nginx       | Plex Media Server |
| 25564| TCP      | nginx       | Minecraft (Bedrock/secondary) |
| 25565| TCP      | nginx       | Minecraft (Java) |
| 62941| TCP      | deluge (via Mullvad) | BitTorrent |

### Outbound (Internal → Internet)

All local VLANs can reach the internet. Some traffic is routed through Mullvad VPN for privacy.

Exception: excel-vm (`excel_vm_address`, VLAN 3) routes via WAN for client M365 sign-in. A mangle `RETURN` skips the Mullvad mark for that one host and a FORWARD rule allows it out the WAN interface; every other VLAN 3 host stays marked and kill-switched.

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
| ~~WordPress~~ | `wordpress` | Retired: blog is static files on nginx (docker/wordpress/README.md) |
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

## IPv6

`networking/iptables/rules.v6` is a default-drop filter (INPUT/FORWARD DROP,
OUTPUT ACCEPT), loaded with `fw-apply 6`. INPUT admits loopback, established,
ICMPv6 errors/echo, NDP (hop limit 255), MLD from link-local on the LAN,
Tailscale, SSH on the mgmt VLAN and mDNS from the LAN. FORWARD admits only
established flows plus Tailscale (via its own `ts-forward` hook). The WAN has
no global IPv6 today; if that changes, add the DHCPv6 client rule there.

LAN-side links (`bond0`, `bond0.N`, `vlanN-shim`) run with `IPv6AcceptRA=no`
so the router never takes addresses/routes from RAs sent by LAN devices.

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

**In practice (Docker 29.x):** dockerd still creates its chains and bridge MASQUERADE rules
for the few bridge networks (`docker0` for the CI dagger engine, nextcloud/wordpress internals),
and Tailscale (`ts-*`, CONNMARK in mangle) and libvirt (`LIBVIRT_*`) add theirs too. A plain
`netfilter-persistent reload` wipes all of them, so the deploy loads `rules.v4` with
`/usr/local/sbin/fw-apply` (`networking/iptables/fw-apply`) instead: it merges the live
foreign chains back into the managed ruleset and swaps it in atomically, without restarting
Docker. Useful by hand:

```bash
fw-apply 4 /etc/iptables/rules.v4 --diff      # what would change vs. live (scratch netns)
fw-apply 4 --rollback                         # restore the ruleset saved before the last apply
```

**If Docker networking breaks after a change**, check that the custom iptables rules in
`networking/iptables/rules.v4` cover any new forwarding needs — don't re-enable Docker iptables
management, as it will conflict with the existing ruleset.

### GitLab CI job network

Jobs from MR and branch pipelines run on the `main.nkontur.com` runner in the Docker
network `gitlab-ci` (bridge `br-ci`, 172.28.0.0/24, created by `configure-docker`); jobs from
`main` run on the `router-protected` runner with host networking (see
`base/gitlab-runner/config.toml`). Rules for `br-ci` in `rules.v4`:

| Table / chain | Rule |
|---|---|
| raw `PREROUTING` → `CI-EGRESS` | allow Pi-hole :53, lab_nginx :443, Vault :8200; drop the router's own addresses, broadcast/multicast, RFC1918, 100.64.0.0/10, 169.254.0.0/16 |
| filter `INPUT` | drop everything from `br-ci` |
| filter `FORWARD` | same allowlist + `-o` WAN, then drop |
| nat `POSTROUTING` | MASQUERADE 172.28.0.0/24 leaving `br-ci` |

The allowlist sits in the raw table because the filter hooks that fw-apply keeps at the top
of FORWARD/INPUT (`DOCKER-*`, `ts-forward`, `ts-input`, `LIBVIRT_*`) run before the managed
filter rules; `ts-forward`, for example, accepts anything routed out `tailscale0`.

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

---

## Rule Categories

### INPUT Chain

Controls traffic TO the router itself:

| Purpose | Interface | Ports |
|---------|-----------|-------|
| SSH | mgmt, tailscale | 22 |
| SSH (rate-limited) | WAN | 22 |
| ICMP | any | - |
| NFS | mgmt, iot | 2049, 111 |
| SMB | mgmt | 139, 445 |
| TFTP | mgmt | 69 |
| mDNS | !internet | 5353 |

### FORWARD Chain

Controls traffic THROUGH the router:

1. **Established/Related** — Always allowed (stateful)
2. **Trusted networks** — mgmt VLAN (full access)
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
| 2026-10 | excel-vm routes via WAN for client M365 sign-in |
