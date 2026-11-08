# Whole-house audio

Snapcast (`snapserver`, 10.6.32.2 on the IoT VLAN) plays one stream per zone
to that zone's `snapclient`. The physical side (USB cards, Monoprice amps,
speaker zones) is data in `inventory/hardware.yml`; issue #154 plans the
multichannel interface that replaces the USB cards.

```
source (Spotify Connect, AirPlay, Mopidy, Bluetooth, tcp)
  -> snapserver stream (Combo_<Zone>, a meta stream)
    -> snapclient_<zone> -> output (USB card today, #154 later)
      -> amp input -> speaker zone (inventory/hardware.yml amps)
```

## Streams and sources

`docker/snapserver/snapserver.conf`. Each zone's group plays the meta stream
`Combo_<Zone>` (Global: `Combo`). A meta stream plays the first of its sources
that is playing, so the order is the priority:

| Meta stream | Sources, highest priority first |
|---|---|
| `Combo` (Global) | `Network`, Mopidy, `Spotify_Global`, Bluetooth |
| `Combo_<Zone>` | `Network_<Zone>`, `Network`, Mopidy, `Spotify_Global`, `Spotify_<Zone>` |
| `Combo_Guest_Bathroom` | `Network_Movie_Room` first (2022, voice feedback from the movie room is heard there), then as above |

`Network*` are `tcp://` server sources, one sender at a time per port
(80 = `Network`, 81-87 = the zones). Home Assistant's doorbell sound
(`sound_doorbell` in `configuration.yaml`) sends to port 80, the AirPlay
receivers below send to their zone's port. A zone's own AirPlay therefore
beats Global AirPlay, Mopidy and Spotify in that zone, and Global AirPlay
(House) beats everything but the zone's own AirPlay.

## AirPlay receivers

One AirPlay 2 receiver (`mikebrady/shairport-sync`) per zone, plus House for
Global. They appear in the AirPlay menu of phones and Macs on VLANs 3 and 4.

| Receiver (AirPlay name) | Container | Address | Snapcast source (port) | Plays in |
|---|---|---|---|---|
| House | `airplay_house` | 10.6.32.24 | `Network` (80) | Global and every zone (`Network` is in every Combo) |
| Movie Room | `airplay_movie_room` | 10.6.32.25 | `Network_Movie_Room` (81) | Movie Room, and Guest Bathroom (see above) |
| Office | `airplay_office` | 10.6.32.26 | `Network_Office` (82) | Office |
| Kitchen | `airplay_kitchen` | 10.6.32.27 | `Network_Kitchen` (83) | Kitchen |
| Main Bedroom | `airplay_main_bedroom` | 10.6.32.28 | `Network_Main_Bedroom` (84) | Main Bedroom |
| Main Bathroom | `airplay_main_bathroom` | 10.6.32.29 | `Network_Main_Bathroom` (85) | Main Bathroom |
| Guest Bathroom | `airplay_guest_bathroom` | 10.6.32.30 | `Network_Guest_Bathroom` (86) | Guest Bathroom |
| Guest Bedroom | `airplay_guest_bedroom` | 10.6.32.31 | `Network_Guest_Bedroom` (87) | Guest Bedroom |

The data is `airplay_receivers` in `ansible/inventory.yml`.

**Silent zones.** Only Global (USB card) and the Movie Room (zwave Pi) have
a snapclient today; the other six are in `snapclient_disabled_zones` until
#154's interface lands. Their receivers work (the stream goes to `playing`),
but nothing listens, so nothing is heard. House plays wherever there is a
client.

### How several AirPlay 2 receivers share one host

AirPlay 2 needs NQPTP, which must own UDP 319/320, and each receiver needs its
own device ID. Each receiver is therefore its own container on the `iot`
macvlan network with its own IP and a fixed MAC: its own network namespace
(own NQPTP, own 319/320, own avahi publishing its own `airplay-<zone>.local`),
and the MAC is its AirPlay device ID (`airplay_device_id`), so the ID survives
recreates. The router's avahi reflector carries the `_airplay._tcp` and
`_raop._tcp` records to VLANs 3 and 4.

Audio path, per receiver (`docker/airplay/`, rendered per receiver into
`/persistent_data/application/airplay/<zone>/`):

- shairport-sync's `pipe` backend writes 48000:16:2 (snapserver's default
  sample format, which the `Network*` sources use) to a FIFO.
- `feed.sh` runs as shairport-sync's play hooks: at play start it connects the
  FIFO to the zone's snapserver port with `nc`, at play end it closes the
  connection. An idle connection would hold the port, because a `tcp://`
  source serves one sender at a time (the doorbell sound on port 80 would
  wait). Pausing ends play, so the stream goes idle and the zone falls back
  to its next source.
- The container's entrypoint keeps the FIFO open (fd 3), so shairport-sync can
  open and write it whether or not `nc` is running.

Container hardening: read-only root (tmpfs `/run`, `/tmp`), `cap_drop: ALL`
plus SETUID/SETGID/CHOWN (dbus-daemon and avahi-daemon drop privileges),
no-new-privileges, 96M. Ports below 1024 need no capability (Docker sets
`ip_unprivileged_port_start=0`). The ephemeral port range is 7100-7199
(sysctl), so the firewall can name AirPlay 2's dynamic ports.

Firewall (`networking/iptables/rules.v4`, docs/FIREWALL.md): VLAN 4 has full
forward access; from VLAN 3, TCP 7000 and 7100-7199 and UDP 319, 320,
6001-6010 and 7100-7199 to `airplay_receivers_cidr`. Classic AirPlay
(AirPlay 1) senders get timing and resend requests from the receiver on their
own UDP ports, so the receivers may send UDP to VLANs 3 and 4.

Version: 5.5.2. 5.6.1 and 5.6.2 exit when a classic sender offers a format
they cannot decode (pyatv sends L16), so Renovate skips 5.6-5.6.2
(`renovate.json`).

### Home Assistant

Each receiver publishes what it plays over MQTT (TLS, user `airplay`,
password in Vault `homelab/docker/airplay` `mqtt_password`) under
`shairport/<zone>/` (`playing`, `active`, `artist`, `title`, `client_name`,
`volume`, ...) and announces itself through MQTT discovery: one device per
receiver (`shairport-sync_<device ID>`) with sensors for artist, album, title,
client and so on. Remote control over MQTT (`enable_remote`) is off: it goes
through the sender's DACP port, which the firewall does not open from the IoT
VLAN.

### Home app and Siri

shairport-sync documents that AirPlay 2 receivers can be added to the Home
app (its ADDINGTOHOME.md). A speaker in the Home app sits in a room, so Siri
can address it by room ("play jazz in the Kitchen"). Not yet tried here; menu
names change between iOS versions. Steps, on an iPhone that belongs to the
Home:

1. Home app -> *Home Settings* -> *Speakers & TV*: *Allow Speaker & TV
   Access* to *Anyone On the Same Network* (or *Everyone*), no password.
2. *+* -> *Add Accessory* -> *More options...*.
3. Pick the receiver (e.g. *Kitchen*), assign the room, finish.
4. Repeat per receiver. House is best put in a room of its own (it plays
   everywhere).

Siri requests made away from the phone (HomePod, Apple TV) go through the
home hub, the Apple TV on the IoT VLAN, which reaches the receivers directly.
The Home app keys a speaker on its device ID (the MAC): keep the MAC when
moving a receiver, or remove and re-add it.

### Adding a zone

1. Snapcast: a `Network_<Zone>` tcp source on the next port and a
   `Combo_<Zone>` meta stream with it first (`snapserver.conf`), the group in
   `server.json`, the snapclient and its card (`inventory/hardware.yml`).
2. Firewall: open the new port if it is outside 80-87 (the snapserver rule).
3. AirPlay: an `airplay_<zone>` entry in `airplay_receivers` (next free
   address in `airplay_receivers_cidr`, widen the CIDR if it is full; MAC
   `02:00:` + the address in hex; the port), and an `airplay_<zone>` service
   in `docker/docker-compose.yml` (copy one, `<<: *airplay`).
4. Home app: add the new speaker (above).

### Checks

```sh
avahi-browse -rpt _airplay._tcp | grep '^='   # on the router: every receiver, 10.6.32.24-31
docker ps --filter name=airplay_               # all healthy
# stream state while something plays (JSON-RPC):
curl -s -d '{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}' http://10.6.32.2:1780/jsonrpc \
  | jq -r '.result.server.streams[] | select(.id|test("Network")) | "\(.id) \(.status)"'
```

A silent end-to-end test needs an AirPlay sender: pyatv (`atvremote
--manual --address <receiver> --port 7000 --protocol raop stream_file=...`)
streams classic AirPlay, but sends L16, which shairport-sync 5.x does not
decode; it needs ALAC (Apple's senders always send ALAC or AAC).
