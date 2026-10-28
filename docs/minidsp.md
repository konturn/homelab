# miniDSP 2x4HD units (movie room)

Two miniDSP 2x4HD units are connected to the USB of the Z-Wave Pi
(`zwave.lab.nkontur.com`, 10.6.128.6, Raspberry Pi 4). `minidspd` from
minidsp-rs 0.1.7 runs on the Pi as the `minidsp` systemd service, installed
and configured by `ansible/roles/configure-zwave` from
`base/minidsp/config.toml`:

- HTTP API on `0.0.0.0:5380` (used by Home Assistant and telegraf)
- plugin-compatible TCP server on `127.0.0.1:5333`
- unix socket `/tmp/minidsp.sock` (used by the `minidsp` CLI on the Pi)

## Safety rule: no writes to the units

The DSP tuning on the units cannot be read back and there is no backup (see
`base/minidsp/configs/README.md`). A wrong write can lose it or damage
speakers. Without Noah present:

- Do not run `minidsp` set commands: `gain`, `mute`, `config` (preset),
  `source`, `output`, `input`, `dirac`, `debug send`, `set-serial`.
- Do not POST to the minidspd HTTP API (`/devices/<n>`, `/devices/<n>/config`).
- Do not play audio into the miniDSP USB sound cards.
- Do not trigger the bass-boost automation or call `rest_command.bass_boost` /
  `bass_boost_2` or `script.minidsp_apply_bass_boost_preset` (flip
  `input_boolean.bass_boost`) to test something. Home Assistant already does
  the only writes that are expected: the bass-boost automation and its sync
  check, both of which only select the preset that matches the boolean.
- Do not run `udevadm trigger --action=add` for the sound subsystem. The add
  event re-runs `90-alsa-restore.rules`, which writes mixer values to the
  units' USB audio interfaces.

Read-only operations are fine; see [Read-only commands](#read-only-commands).

## Device map

| Serial | USB path | ALSA id (stable, after next boot) | ALSA id today (by enumeration) | minidspd index today | Role (unconfirmed) |
|---|---|---|---|---|---|
| 919619 | `1-1.3` | `minidsp_919619` | `m2x4HD_1` (card 2) | 0 | Outputs 3-4 sit 13-19 dB below the noise floor of outputs 1-2; possibly muted or unused there |
| 921284 | `1-1.2` | `minidsp_921284` | `m2x4HD` (card 1) | 1 | Live signal on analog input 1 and on all 4 outputs |

Checked on 2026-10-28. The two "today" columns follow USB enumeration order and
can swap after a reboot or replug; note that index 0 is currently the unit on
the *higher* USB port. The roles come from read-only level meters only.
Which unit drives which speakers has not been confirmed (needs Noah).

How the map was derived (all read-only):

- `curl -s localhost:5380/devices` lists each unit's serial with its USB
  path, e.g. `"url": "usb:1-1.3%3A1.4?vid=2752&pid=0011"` with
  `"serial": 919619`. The order of that list is the device index.
- `/proc/asound/cards` and `readlink -f /sys/class/sound/cardN` give each ALSA
  card's USB path (`.../usb1/1-1/1-1.3/1-1.3:1.0/sound/card2`).
- The units report no USB serial descriptor (`iSerial 0` in `lsusb -v`, no
  `serial` attribute in sysfs), so udev cannot match on the serial directly.

The single source of truth is `minidsp_units` in `ansible/inventory.yml`
(serial and USB path). If a unit is moved to another USB port, update its
`usb_path` there.

## Stable addressing

### minidspd HTTP API: by serial

minidsp-rs 0.1.7 has no config option to name or pin devices in the HTTP API
(`[[static_device]]` only adds a device by URL; `device_serial` /
`device_index` only bind a `[[tcp_server]]`). It does not need one: the
`:deviceIndex` path parameter is matched against each device's serial first
and only then used as an index (`get_device()` in
`daemon/src/http/mod.rs`). So `/devices/919619` and `/devices/919619/config`
always address unit 919619, whatever its index. A serial that is not connected
returns HTTP 500 `DeviceIndexOutOfRange` (serials are 6-digit numbers, far
above any index), so a request can never land on the other unit.

Everything in this repo addresses the units by serial: the Home Assistant
REST commands and sensors, and telegraf.

The plugin TCP server (`127.0.0.1:5333`) is not pinned and serves the first
device. Before using the plugin app against a specific unit, add
`device_serial = <serial>` to its `[[tcp_server]]` block (see the comments in
`base/minidsp/config.toml`). Changing `config.toml` restarts `minidspd`.

### ALSA cards: udev rule by USB port

`base/minidsp/85-minidsp-alsa-ids.rules` (templated from `minidsp_units` to
`/etc/udev/rules.d/` on the Pi) sets the card id to `minidsp_<serial>` by
matching the USB port. Use `plughw:CARD=minidsp_<serial>,DEV=0` in any ALSA
client.

- The rule takes effect on the card's next `add` event: the next reboot of the
  Pi, or a replug of the unit. The deploy only reloads the rules; it does not
  trigger udev (see the safety rule above).
- alsa-restore looks the saved mixer state up by card id. The role seeds
  `state.minidsp_919619` and `state.minidsp_921284` in
  `/var/lib/alsa/asound.state` (template `base/minidsp/asound-state-minidsp.conf`)
  with exactly the values the `m2x4HD` / `m2x4HD_1` sections already held
  (identical for both: playback and capture switches on, volumes 127 = 0 dB).
  At boot the units therefore get the same mixer writes as before the rename.

After the next reboot, check:

```bash
ssh zwave.lab.nkontur.com 'cat /proc/asound/cards'
#  1 [minidsp_921284 ]: USB-Audio - miniDSP 2x4HD ... at usb-0000:01:00.0-1.2
#  2 [minidsp_919619 ]: USB-Audio - miniDSP 2x4HD ... at usb-0000:01:00.0-1.3
```

## Home Assistant

- `rest_command.bass_boost` POSTs `{"master_status": {"preset": 0|1}}` to
  `http://zwave.lab.nkontur.com:5380/devices/919619/config` and
  `rest_command.bass_boost_2` the same to `/devices/921284/config`. The preset
  is 0 when `input_boolean.bass_boost` is off (normal) and 1 when it is on
  (bass boost), on both units. They are defined in the repo-managed
  `docker/homeassistant/configuration.yaml`; before 2026-10-28 they used
  indexes 0 and 1.
- Everything that calls them is repo-managed in the Home Assistant package
  `docker/homeassistant/packages/minidsp_bass_boost.yaml`
  (`docs/HOMEASSISTANT.md`, *Packages*). Until 2026-10-28 the automation was a
  UI automation in `automations.yaml` that called the two units one after the
  other, so a failed first call skipped the second unit, and nothing checked
  afterwards.

### Switching

The automation "Change Minidsp on base boost" (`id: '1665111219536'`, kept
from the UI version) runs on every off -> on / on -> off change of
`input_boolean.bass_boost`. It runs
`script.minidsp_apply_bass_boost_preset` for both units in `parallel:`, each
with `continue_on_error: true`, so one unit failing does not affect the other.
`mode: queued`: a toggle during a run is applied after it, not dropped.

`script.minidsp_apply_bass_boost_preset` (field `serial`) maps the serial to
its rest_command, reads the boolean (and sends nothing if it is not `on` or
`off`), and runs `script.minidsp_bass_boost_attempt` up to twice, 5 s apart.
One attempt:

1. calls the unit's rest_command (`continue_on_error: true`);
2. reads the preset back with `rest_command.minidsp_status`
   (`GET /devices/<serial>`, read-only);
3. refreshes `sensor.minidsp_<serial>_preset` (`homeassistant.update_entity`,
   another read-only GET);
4. returns `ok` only if the read-back is HTTP 200 and shows the expected
   preset.

The read-back decides, not the write's response, because in Home Assistant
2026.10 `rest_command` only raises (`HomeAssistantError`) on timeouts,
connection errors and responses it cannot decode. An HTTP error status (for
example 500 `DeviceIndexOutOfRange` when the unit is not connected) is only
logged as a warning; the call returns normally with `status` in its response.
After two failed attempts the script logs an error
(`homeassistant.minidsp_bass_boost`) and creates the persistent notification
`minidsp_<serial>_preset`, which the next success dismisses.

### Sync check and mismatch sensors

| Entity | On when |
|---|---|
| `binary_sensor.minidsp_919619_preset_mismatch`, `binary_sensor.minidsp_921284_preset_mismatch` | that unit's preset sensor has not matched the boolean (0 off, 1 on) for 2 minutes (`delay_on`). Unavailable while the preset sensor or the boolean is unavailable/unknown. |
| `binary_sensor.minidsp_preset_mismatch` | a readable unit has been mismatched for 10 minutes. An unreadable unit does not count (that is the `minidsp_units` anomaly signal). |

The 2-minute delay covers the 60 s poll of the preset sensors: right after a
switch a unit can look mismatched for up to a minute.

The automation "miniDSP bass boost sync check" (`id: minidsp_bass_boost_sync`)
triggers on HA start, on any change of either preset sensor or per-unit
mismatch sensor, and every 5 minutes. It runs only if a per-unit mismatch
sensor is on and it has not run in the last 10 minutes (the cooldown is a
condition, so `last_triggered` moves only when it re-sends). It then runs
`script.minidsp_apply_bass_boost_preset` for the mismatched unit(s) only,
which sends the preset that matches the boolean at that moment. `mode:
single`. A unit that stays wrong is retried at most every 10 minutes, and
`binary_sensor.minidsp_preset_mismatch` turns on.
- Read-only status, one REST resource per unit (`GET /devices/<serial>` every
  60 s):

  | Entity | Value |
  |---|---|
  | `sensor.minidsp_919619_preset`, `sensor.minidsp_921284_preset` | active preset (0 normal, 1 bass boost) |
  | `sensor.minidsp_919619_source`, `sensor.minidsp_921284_source` | input source (`Analog`, `Toslink`, `Usb`) |
  | `sensor.minidsp_919619_volume`, `sensor.minidsp_921284_volume` | master volume, dB |
  | `binary_sensor.minidsp_919619_mute`, `binary_sensor.minidsp_921284_mute` | master mute |

  They become unavailable when a unit is not connected.

## Metrics and alerting

telegraf on the Pi (`docker/telegraf/satellite-telegraf.conf`, zwave only)
reads `GET http://127.0.0.1:5380/devices/<serial>` every 60 s into the
`metrics` bucket, measurement `minidsp`, tags `serial` and `usb_path`:

| Field | Meaning |
|---|---|
| `preset` | active preset |
| `source` | input source |
| `volume_db` | master volume |
| `mute` | master mute |
| `input_1_level_db`, `input_2_level_db` | input meters, dBFS |
| `output_1_level_db` ... `output_4_level_db` | output meters, dBFS |

The anomaly detector's `minidsp_units` signal (moderate only, digest) fires
when a serial has written no point for 15 minutes: the unit dropped off USB,
lost power, or `minidspd` stopped.

The `minidsp_preset_mismatch` signal (moderate only, digest) fires while
`binary_sensor.minidsp_preset_mismatch` is on (`homeassistant` bucket,
measurement `binary_sensor.minidsp_preset_mismatch`, field `value`): a unit has
been on the wrong preset for 10 minutes and the HA sync check could not fix
it.

Example Flux:

```flux
from(bucket: "metrics")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "minidsp" and r._field =~ /level_db$/)
```

## Read-only commands

On the Pi:

```bash
minidsp probe                                  # serials and current indexes
curl -s localhost:5380/devices                 # serial <-> USB path <-> index
curl -s localhost:5380/devices/919619          # preset, source, volume, mute, levels
curl -s localhost:5380/devices/921284
cat /proc/asound/cards                         # ALSA ids and USB paths
readlink -f /sys/class/sound/card1             # card -> USB path
journalctl -u minidsp --no-pager | tail -50
```

`minidsp debug dump` / `debug dump-float` are also read-only. Pass the unit
explicitly with `--url` when using the CLI against one unit.

## Movie-room snapclient

The `movie` snapclient on the Pi (`snapclient_config` in
`ansible/inventory.yml`) still targets `plughw:CARD=b1,DEV=0`, a card that no
longer exists, and stays `enabled: false`. Once Noah confirms which unit
carries the movie-room speakers, point it at
`plughw:CARD=minidsp_<serial>,DEV=0` and re-enable it. Playing audio into a
unit is covered by the safety rule above, so do that with Noah present.

## Open items for Noah

- Which unit drives which speakers (the roles above are unconfirmed), and
  whether 919619's outputs 3-4 are muted on purpose.
- The original plugin XML files for both units
  (`base/minidsp/configs/README.md`).
- The snapclient target, above.
