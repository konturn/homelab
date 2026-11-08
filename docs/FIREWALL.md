# Firewall Architecture

This document describes the iptables firewall configuration for the homelab infrastructure.

**Last Audit:** November 2026  
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
| 7    | 10.7.0.0/16  | Reserved (DHCP only)             | —              |

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
| 443  | TCP      | nginx       | HTTPS (reverse proxy; some vhosts LAN + tailnet only, below) |
| 32400| TCP      | nginx       | Plex Media Server |
| 62941| TCP      | deluge (via Mullvad) | BitTorrent |

The game-server forwards (25564-25566) were removed: nothing listened behind them.
Game servers, if any come back, are reached over Tailscale (friends need the node
shared with them) rather than a port forward.

**Public vs. LAN + tailnet only** (nginx `allow 10.0.0.0/8; allow 100.64.0.0/10; deny all;`):

| Public | LAN + tailnet only |
|--------|--------------------|
| Nextcloud, Plex (`:32400`, `/plex/`), Ombi (`/plex-requests/`, used by friends without Tailscale), blog, `nkontur.com` site, Telegram webhook (Telegram ranges only) | Vaultwarden (`bitwarden.`), Audioserve (`booksonic.`), Home Assistant (`homeassistant.`) |

WAN SSH to the router stays (key-only, rate-limited) as the out-of-band path for when
Tailscale itself is down; tailscaled runs on the router, so it cannot be its own fallback.

### Outbound (Internal → Internet)

All local VLANs can reach the internet. Some traffic is routed through Mullvad VPN for privacy.

Exception: excel-vm (`excel_vm_address`, VLAN 3) routes via WAN for client M365 sign-in. A mangle `RETURN` skips the Mullvad mark for that one host and a FORWARD rule allows it out the WAN interface; every other VLAN 3 host stays marked and kill-switched.

Exception: Home Assistant (`homeassistant_ip`, VLAN 3) reaches a short list of vehicle-service hosts via WAN; see [WAN bypass for Home Assistant](#wan-bypass-for-home-assistant). The rest of its traffic stays on Mullvad.

### WAN bypass for Home Assistant

**Why:** the connected-vehicle services used by the `toyota_connected` integration (Toyota, Lexus and Subaru/Solterra share one platform) reject VPN egress: the ForgeRock sign-in endpoint answers `403` from Mullvad exits and `200` from the home WAN address. Home Assistant cannot sign in through Mullvad.

**What is bypassed:** only traffic *from Home Assistant's internal address* (`homeassistant_ip`, pinned in `ansible/inventory.yml` and `docker-compose.yml`) *to the current addresses of the listed hostnames*. Nothing else on VLAN 3/6 changes, and HA's other internet traffic still exits via Mullvad.

| Host | Used for |
|------|----------|
| `login.subarudriverslogin.com` | Sign-in (Subaru / Solterra) |
| `login.toyotadriverslogin.com` | Sign-in (Toyota) |
| `login.lexusdriverslogin.com` | Sign-in (Lexus) |
| `onecdn.telematicsct.com` | REST API gateway |
| `oa-api.telematicsct.com` | GraphQL API (HTTPS and realtime websocket) |

The list lives in `networking/iptables/wan-bypass-ha.hosts`. The integration's EU endpoints (`*.toyota-europe.com`, `*.toyotaconnectedeurope.io`) are not listed; they are only used for EU accounts.

**How it works:**

1. ipset `wan_bypass_ha` (`hash:ip`, 1 h timeout per entry) is declared in `rules.ipset`. The deploy creates it before `rules.v4` is validated, because `iptables-restore` refuses a ruleset that references a missing set; at boot `netfilter-persistent` creates it (plugin `10-ipset`) before loading `rules.v4`.
2. `wan-bypass-update` (`/usr/local/sbin`, run by `wan-bypass-update.timer` every minute and 30 s after boot) resolves each hostname through the router's resolver (Pi-hole, the same resolver HA uses through Docker's embedded DNS), keeps only public unicast IPv4 answers, and adds them with a 1 h timeout (re-adding resets it). The hosts sit behind AWS load balancers and CloudFront with 0-60 s TTLs, hence the short interval; addresses that stop being handed out age out.
3. mangle PREROUTING: `-s homeassistant_ip -i vlan3-shim -m set --match-set wan_bypass_ha dst -j RETURN`, before the VLAN 3 Mullvad mark. The packet stays unmarked, so it uses the `main` table (default route via WAN) instead of table `mullvad`.
4. filter FORWARD: `-s homeassistant_ip -i vlan3-shim -o WAN -m set --match-set wan_bypass_ha dst -j ACCEPT`, after the kill switch. The kill switch (`REJECT` for mark `0x1` out the WAN) does not match because the packet is unmarked.
5. nat POSTROUTING: the existing `-o WAN -j MASQUERADE` applies, as for VLAN 2/4/5.

**Adding a host:** add the hostname to `wan-bypass-ha.hosts` (exact names only, no wildcards) with a comment saying what uses it, and merge. The deploy installs the list and runs `wan-bypass-update` once. Only add hosts that HA itself contacts and that demonstrably fail through Mullvad.

**Checking it:**

```bash
ipset list wan_bypass_ha                     # current addresses + remaining timeout
wan-bypass-update --dry-run                  # what the hostnames resolve to now
systemctl status wan-bypass-update.timer
journalctl -u wan-bypass-update -n 20
iptables -t mangle -vnL PREROUTING           # RETURN counter for wan_bypass_ha
```

### IoT DNS

IoT devices must not learn internal DNS names, so they resolve through
iot-dns (`iot_dns_ip`), which forwards to Pi-hole and answers the internal
zones NXDOMAIN (docs/NETWORK.md, "IoT resolver"). DHCP hands out only
iot-dns on VLAN 6. The firewall makes sure devices cannot go around it:

| Table / chain | Rule |
|---|---|
| nat `PREROUTING` → `IOT-DNS` | udp/tcp 53 from IoT devices (10.6.64.0–10.6.255.255): the two Pis `RETURN`, traffic already for iot-dns `RETURN`, everything else DNAT to `iot_dns_ip:53` (Pi-hole, 1.1.1.1, hardcoded resolvers) |
| mangle `PREROUTING` → `IOT-DNS` | the same packets (except the Pis) `ACCEPT` before the VLAN 6 Mullvad mark: after the DNAT they are for iot-dns, and table `mullvad` would route a marked packet into the tunnel |
| nat `POSTROUTING` | MASQUERADE redirected flows (`--ctstate DNAT`) to iot-dns out `vlan6-shim` |
| filter `FORWARD` | accept the redirected flows (`--ctstate DNAT`) to iot-dns:53; the Pis to Pi-hole:53; drop anything else from IoT devices to Pi-hole; reject tcp/udp 853 (DoT, DoQ) from IoT devices |

Matching: IoT devices are matched by **source address** (VLAN 6 outside the
docker range 10.6.0.0/18, i.e. the DHCP pool and the 10.6.128.x
reservations), not by interface. Containers on the iot docker network
(10.6.0.0/18) arrive on `vlan6-shim` and are never redirected, so container
DNS and iot-dns's own queries to Pi-hole are untouched. Devices mostly arrive
on `bond0.6`, but `vlan6-shim` also answers ARP for the gateway 10.6.0.1
(ARP flux), so some devices send through the shim; an `-i bond0.6` match
missed them in the first version of these rules.
Devices that ask iot-dns directly do not pass the router at all (same L2).

**Why MASQUERADE here** (the [hairpin](#nat-table) needs none): the device
and iot-dns share a subnet, so without it iot-dns would answer the device
directly from its own address, and the device, which asked 1.1.1.1, would
drop the reply. Masqueraded to the shim address (10.6.0.2), the reply comes
back through the router and is un-DNATed. Pi-hole logs those redirected
queries as coming from 10.6.0.2 (they are rare: devices use the DHCP
resolver).

**Limits:** DNS over HTTPS is not blocked (no DoH blocklist); a device with
built-in DoH resolves around iot-dns and Pi-hole. Hardcoded IPs need no DNS.

### IoT devices without internet

ipset `iot_no_wan` (`hash:ip`, members rendered into `rules.ipset` from the
DHCP reservations) holds IoT devices that work without the internet. FORWARD
rejects anything they send out via Mullvad or the WAN, ahead of
`RELATED,ESTABLISHED` so existing sessions stop too. LAN flows (Frigate, Home
Assistant, NTP on the router at 10.6.0.1) are unaffected.

| Device | Variable | Lost without internet |
|---|---|---|
| Back camera | `back_camera_address` | Amcrest app remote view (P2P is also off on the camera); NTP now from the router |
| Projector | `projector_address` | nothing in use |
| Brother printer | `brother_printer_address` | firmware-update checks, Brother cloud/mobile print relay |
| NETGEAR GS308EP | `iot_poe_switch_address` | firmware checks |
| SLZB-MR1 Zigbee coordinator | `zigbee_radio_address` | OTA checks; `binary_sensor.slzb_mr1_internet` goes off |

Not in the set yet (they still use the cloud): doorbell (offline), Frame TV,
weather station. Must keep the cloud: the PETLIBRO feeder (feeding schedule
and clock over its MQTT session), the Litter-Robots, Miele, Thermador, Denon,
Apple TV.

**Adding a device:** give it a DHCP reservation (`ansible/inventory.yml`,
`networking/dhcp/dhcpd.conf`), add `add iot_no_wan {{ <device>_address }}` to
`rules.ipset`, list it here, merge. The deploy reloads the ipsets.

### Inter-VLAN Traffic

| Source | Destination | Policy |
|--------|-------------|--------|
| mgmt (10.4.x.x) | Any | **ALLOW ALL** (trusted) |
| internal (10.3.x.x) | mgmt | SSH, ICMP only |
| iot devices (10.6.64.0–10.6.255.255) | iot-dns | DNS; any other DNS is redirected there, Pi-hole is dropped, 853 refused ([IoT DNS](#iot-dns)) |
| iot containers (`vlan6-shim`), zwave and satellite-2 Pis | Pi-hole | DNS only |
| iot devices in `iot_no_wan` | internet | none ([IoT devices without internet](#iot-devices-without-internet)) |
| iot Pis (zwave, satellite-2) | Vault | TCP 8200 |
| Any | snapserver | Service ports only (1704, 1705, 1780, 80-87, 50000-50007) |
| internal (10.3.x.x) | AirPlay receivers (`airplay_receivers_cidr`) | TCP 7000, 7100-7199; UDP 319, 320, 6001-6010, 7100-7199 (docs/AUDIO.md) |
| AirPlay receivers | internal, mgmt | UDP to ports >= 1024 (classic AirPlay timing and resend requests) |
| frigate (`frigate_ip`, internal) | back camera, doorbell (iot) | TCP 80, 554 |
| frigate (`frigate_ip`, internal) | mosquitto (iot) | TCP 8883 |
| hcpy (`hcpy_ip`, internal) | Thermador hood and range (iot) | TCP 80 |
| hcpy (`hcpy_ip`, internal) | mosquitto (iot) | TCP 8883; anything else from hcpy is refused |
| Apple TV | internet, lab_nginx | Internet + TCP 32400 (Plex) |

---

## Services by Network

### External Network (10.2.x.x)

Internet-facing services proxied through nginx:

| Service | Container | Purpose | Reachable from |
|---------|-----------|---------|----------------|
| nginx | `nginx` | Reverse proxy, SSL termination | internet |
| Plex | `plex` | Media streaming | internet |
| Bitwarden | `bitwarden` | Password manager | LAN + tailnet |
| Nextcloud | `nextcloud` | File sync/share | internet |
| ~~WordPress~~ | `wordpress` | Retired: blog is static files on nginx (docker/wordpress/README.md) | internet (blog) |
| Ombi | `ombi` | Plex requests | internet |
| AudioServe | `audioserve` | Audiobook streaming | LAN + tailnet |
| Home Assistant | `homeassistant` | Smart home (also on internal/iot) | LAN + tailnet |

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
| **OpenClaw** (moltbot) | `openclaw-gateway` | AI assistant |

### IoT Network (10.6.x.x)

Smart home and media devices:

| Service | Container/IP | Purpose |
|---------|--------------|---------|
| Home Assistant | `homeassistant` | Smart home hub |
| Mosquitto | `mosquitto` | MQTT broker |
| Zigbee2MQTT | `zigbee2mqtt` | Zigbee gateway |
| Snapserver | `snapserver` | Multi-room audio server |
| Snapclients | `snapclient_*` | Audio zone endpoints |
| AirPlay receivers | `airplay_*` | AirPlay 2 into snapserver, one per zone (docs/AUDIO.md) |
| Mopidy | `mopidy` | Music player |
| iot_nginx | `iot_nginx` | IoT reverse proxy |
| Amcrest2MQTT | `amcrest2mqtt` | Doorbell bridge |
| AmbientWeather | `ambientweather` | Weather station bridge |

### Management Network (10.4.x.x)

Infrastructure services:

| Service | Container | Purpose |
|---------|-----------|---------|
| noVNC | `novnc-dev1`..`novnc-dev3` | Remote desktop access |
| BlueBubbles relay | `bluebubbles-relay` | Messaging relay |

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

## Tailscale

tailscaled runs in netfilter mode `on` and installs its own hooks, which fw-apply keeps
across reloads:

| Chain | Effect |
|-------|--------|
| filter `INPUT` → `ts-input` (first rule) | accept everything from `tailscale0`; accept UDP 41641; drop 100.64.0.0/10 sources on other interfaces |
| filter `FORWARD` → `ts-forward` (first rule) | mark and accept everything from `tailscale0`; accept everything to `tailscale0` |
| nat `POSTROUTING` → `ts-postrouting` | masquerade forwarded tailnet traffic to the router's address on the egress VLAN |

So the managed `100.64.0.0/10` / `tailscale0` rules in `rules.v4` (ICMP, DNS to Pi-hole,
TCP 80/443, `nginx_ip`:443, SSH and DNS to the router) are not what limits tailnet
access today: the tailnet policy file is. They describe the intended policy and take
over if tailscaled is switched to `--netfilter-mode=nodivert` (which also needs a
managed MASQUERADE for forwarded tailnet traffic and an INPUT rule for UDP 41641).
Narrowing access is done in the tailnet policy, for example by limiting a device group
to `10.2.32.1:443` and the Pi-hole on port 53.

The router's own Tailscale traffic (control plane, DERP, WireGuard UDP) is fwmark
`0x80000` and routed via `main` to the WAN; the Mullvad mark `0x1` and the FORWARD kill
switch only apply to forwarded VLAN 3/6/7 traffic. See docs/NETWORK.md ("Remote Access").

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
for the bridge networks (`docker0`, the CI job networks, per-stack internal bridges such as nextcloud, proxy-internal and the OpenClaw ones),
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
`main` run on the `router-protected` runner in `gitlab-ci-protected` (below; see
`base/gitlab-runner/config.toml`). Rules for `br-ci` in `rules.v4`:

| Table / chain | Rule |
|---|---|
| raw `PREROUTING` → `CI-EGRESS` | allow Pi-hole :53, lab_nginx :443, Vault :8200, the canary service (172.28.0.1:8743, [CANARY.md](CANARY.md)); drop the router's own addresses, broadcast/multicast, RFC1918, 100.64.0.0/10, 169.254.0.0/16 |
| filter `INPUT` | accept tcp :8743 to 172.28.0.1 (canary service), drop the rest from `br-ci` |
| filter `FORWARD` | same allowlist + `-o` WAN, then drop |
| nat `POSTROUTING` | MASQUERADE 172.28.0.0/24 leaving `br-ci` |

The protected runner's jobs get their own network `gitlab-ci-protected` (bridge `br-ci-prot`,
172.28.1.0/24, also created by `configure-docker`), with a wider but still explicit allowlist:

| Table / chain | Rule |
|---|---|
| raw `PREROUTING` → `CI-PROT-EGRESS` | allow Pi-hole :53, lab_nginx :443 and :2376 (docker-socket-proxy), Vault :8200; SSH :22 to the router (10.4.0.1), the Pis, the Mac mini, the Aruba Instant VC and APs, the S2500 (10.100.0.1) and the UPS card; NETGEAR switches :80; Denon :23/:8080; then the same drops as `CI-EGRESS` |
| filter `INPUT` | accept tcp :22 to 10.4.0.1 from `br-ci-prot`, drop the rest |
| filter `FORWARD` | same allowlist + `-o` WAN, then drop |
| nat `POSTROUTING` | MASQUERADE 172.28.1.0/24 leaving `br-ci-prot` |

Canary containers ([CANARY.md](CANARY.md)) run in the Docker network `canary` (bridge
`br-canary`, 172.28.9.0/24), which `canary.py` creates `--internal` for each run and removes
afterwards. On top of Docker's own isolation, `rules.v4` drops in raw `PREROUTING` whatever
`br-canary` sends outside its subnet, drops all of it in `INPUT`, and drops `FORWARD` in and
out of the bridge, so a canary reaches only the other canary containers.

Devices therefore see the router's own address on the egress interface, as they did when these
jobs used host networking. The firmware rollout jobs are the exception: their TFTP server must
bind the router's addresses, so they run on the `router-firmware` runner (tag
`firmware-hostnet`), the only runner left on host networking.

The allowlists sit in the raw table because the filter hooks that fw-apply keeps at the top
of FORWARD/INPUT (`DOCKER-*`, `ts-forward`, `ts-input`, `LIBVIRT_*`) run before the managed
filter rules; `ts-forward`, for example, accepts anything routed out `tailscale0`.

---

## Known Issues

### 🔐 Security Recommendations

1. **~~Restrict SSH Input~~** ✅ Partially addressed
   - WAN SSH now has rate limiting (max 4 new connections/60s per source IP)
   - mgmt VLAN and Tailscale access remains unrestricted (trusted networks)
   - Future consideration: remove WAN SSH entirely if Tailscale proves reliable

2. **~~Device-specific rules~~** ✅ Addressed
   - The Shield TV full-access rule is gone; Apple TV is limited to the internet and lab_nginx:32400

---

## Rule Categories

### INPUT Chain

Controls traffic TO the router itself:

| Purpose | Interface | Ports |
|---------|-----------|-------|
| SSH | mgmt, tailscale | 22 |
| SSH (to 10.4.0.1 only) | protected CI network (`br-ci-prot`) | 22 |
| DNS | tailscale | 53 |
| SSH (rate-limited) | WAN | 22 |
| ICMP | any | - |
| NFS | mgmt | 2049 |
| NFS + rpcbind | iot | 111, 2049 |
| SMB | mgmt | 139, 445 |
| NUT (upsd, to 10.4.0.1) | mgmt, internal (Home Assistant) | 3493 |
| NTP (from the S2500, 10.100.0.1) | management link (`enp2s0f1`) | 123/udp |
| TFTP | mgmt | 69 |
| NTP (to 10.6.0.1) | iot (`bond0.6`) | 123/udp |
| mDNS | !internet | 5353 |

### FORWARD Chain

Controls traffic THROUGH the router:

1. **Established/Related** — Always allowed (stateful)
2. **Trusted networks** — mgmt VLAN (full access)
3. **Internet-bound** — All VLANs can reach internet
4. **Inter-VLAN** — Restricted by default
5. **Inbound from internet** — Specific ports to external VLAN
6. **VPN traffic** — Mullvad routing (and Deluge's inbound 62941)

### NAT Table

1. **DNAT (port forwarding)** — Internet inbound to services
2. **SNAT/MASQUERADE** — Outbound NAT for all networks
3. **Hairpin NAT** — internal/mgmt/IoT (VLAN 3/4/6) reaching nginx via the WAN IP (DNAT only, no SNAT)
4. **IoT DNS redirect** — DNS from IoT devices to anything but iot-dns is DNATed to it, and MASQUERADEd because both are on one subnet ([IoT DNS](#iot-dns))

---

## Maintenance

### Adding New Services

1. Determine which network the service should be on
2. If internet-exposed: Add DNAT rule for port forwarding
3. If inter-VLAN access needed: Add FORWARD rule
4. Update this document

### Testing Changes

```bash
# rules.v4 is a Jinja template; the deploy renders it to /etc/iptables/rules.v4.
# On the router, compare the rendered file with the live ruleset:
fw-apply 4 /etc/iptables/rules.v4 --diff

# Apply via Ansible (normally the router:deploy job after merge)
ansible-playbook -i ansible/inventory.yml ansible/router.yml --tags configure-router-network
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
| 2026-10 | Tailscale: role installs from the pinned apt repo; tailnet → nginx:443 rule; ts-* hooks documented |
| 2026-10 | Vaultwarden, Audioserve, Home Assistant restricted to LAN + tailnet (Ombi stays public); game-server and port 22 forwards removed |
| 2026-11 | Home Assistant reaches the connected-vehicle service hosts via WAN (ipset `wan_bypass_ha`); HA's internal address pinned |
| 2026-11 | Docs audit: device rules (Shield TV gone, Apple TV limited), snapserver ports, NTP from the S2500, registry container retired, test/apply commands |
| 2026-11 | IoT devices: DNS only via iot-dns (redirect, Pi-hole dropped, 853 refused); NTP from the router; ipset `iot_no_wan` (back camera, projector, Brother, GS308EP, SLZB) |
