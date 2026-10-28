# Denon AVR-X6700H

The movie-room receiver (`denon.lab.nkontur.com`, 10.6.128.3, IoT VLAN) is
captured as code in `networking/denon/`:

| File | Purpose |
|---|---|
| `denon-x6700h.yaml` | Snapshot of every setting the receiver reports (desired state) |
| `denonctl.py` | `export`, `drift`, `check`, `plan`, `apply` (Python stdlib only) |
| `test_denonctl.py` | Offline unit tests |

Everything except `apply --send` is read-only. It sends only telnet queries
ending in `?` and HTTP `Get*` commands. It never sends power, input, volume,
menu (`MN*`), system/reset (`SY*`) or test-tone commands, and it never
touches the Audyssey calibration.

## Interfaces

| Interface | Port | Used for |
|---|---|---|
| Telnet control protocol | 23 | Nearly everything: `SS*` setup info, `PS*`, `CV`, `VS*`, zones |
| `goform/AppCommand.xml` (POST) | 8080 | Source rename/delete list as the web UI and app see it, zone name, Quick Select names |
| `goform/AppCommand0300.xml` | 8080 | `GetAudyssey` answers but with `control="0"` and no values, so it is not used |
| `description.xml`, `formMainZone_MainZoneXml*.xml` | 8080 | 403 while the web stack is wedged (see below); not used |

The firmware's HTTP parser only accepts AppCommand requests with each XML
element on its own line. A single-line `<tx>...</tx>` body returns an empty
`<rx>`.

On this firmware the telnet port accepted two clients at once (tested
2026-10-28). Sessions are still kept short (one export is about 80 queries,
about 40 s), so firmware or a reset that allows only one client won't
lock Home Assistant out for long.

## What is captured

Values are kept as the receiver reports them, with surrounding blanks
stripped. Levels are converted to dB strings (Denon `50` = 0 dB, `565` = +6.5
dB). Keys are sorted, so a re-export of an unchanged receiver gives an
identical file.

| Section | Source | Contents |
|---|---|---|
| `device` | `NSFRN`, `SSINFFRM` | Friendly name, firmware versions. Information only: not drift-checked, never applied |
| `sources.names` / `sources.use` | `SSFUN ?`, `SSSOD ?` | Source rename names and `USE`/`DEL` (Source Delete) per input code |
| `web` | AppCommand | `sources` (rename and use/deleted as the web UI/app list them, including `AUX2`, which is only deleted there), `zone_names`, `quick_select` (names and stored source) |
| `speakers` | `SSSPC`, `SSCFR`, `SSLEV`, `SSSDE`, `SSSWM`, `SSLFL`, `SSPAA`, `SPPR` | Speaker config/size, crossovers, speaker levels (dB), distances (raw; `STP` is the distance step setting), subwoofer mode, LPF for LFE, amp assign, speaker preset |
| `hdmi` | `SSHOS ?` | HDMI setup (CEC control, power-off control, ARC, pass-through, etc.), keyed as reported |
| `video` | `VSASP`, `VSMONI`, `VSSC`, `VSSCH`, `VSAUDIO`, `VSVPM` | Aspect, monitor out, resolution, HDMI audio out, video processing mode |
| `system` | `ECO`, `DIM`, `STBY`, `SSLOC`, `BTTX` | ECO mode, dimmer, auto standby, setup lock, Bluetooth transmitter |
| `zone2`, `zone3` | `Z2CS`/`Z3CS`, `Z2CV`, `Z2HPF`, `Z2PSBAS`, `Z2PSTRE`, `Z2STBY` | Channel setting, channel levels, HPF, bass/treble, auto standby |
| `per_input.<INPUT>` | `MS`, `SD`, `PSMULTEQ`, `PSDYNEQ`, `PSREFLEV`, `PSDYNVOL`, `PSLFC`, `PSCNTAMT`, `PSGEQ`, `PSTONE CTRL`, `PSBAS`, `PSTRE`, `CV`, `PSSWL`, `PSSWR`, `PSLFE`, `PSDIC`, the other `PS*` surround parameters | Sound mode, input mode, Audyssey MultEQ/Dynamic EQ/reference level offset/Dynamic Volume/LFC/containment amount, graphic EQ, tone control, channel level adjust, subwoofer level, LFE, dialog, Cinema EQ, DRC, Neural:X, Auro-3D, speaker output and similar |

### Per-input settings

The receiver keeps Audyssey, tone, channel level and surround parameters per
input source. Over the network they can only be read for the selected input,
and selecting another input would change the receiver. So:

- `export` writes the selected input's values under `per_input.<INPUT>` and
  keeps the entries for other inputs from the existing file. The snapshot
  fills in as inputs are used: run `export` again while another input is
  selected to add it.
- `drift` compares only the selected input's entry.
- The read checks the input before and after; if it changed during the read,
  the export fails and has to be run again.
- Some surround parameters are reported only for some sound modes. A value
  that is not reported is kept from the file (`export`) or listed as
  `UNREAD` (`drift`), never treated as drift.
- `sound_mode` is exported but not drift-checked, because it changes with the
  remote all the time.

### Runtime state (never in the file)

Power, zone power, selected input, volume, mute, max volume, sleep timer,
Quick Select in use, trigger outputs, and zone 2/3 power, source and volume.
`export --runtime` prints them to stderr.

## What is not captured

- **Audyssey room-correction filters** (the MultEQ XT32 curves and target
  curves). The receiver doesn't expose them over the network. They can only
  be read or restored with the Audyssey MultEQ-X / Editor app and its
  `.ady`/`.mqx` files. Calibration is never run or modified from here.
  `speakers.levels_db`, `distances_raw` and `crossover` are the visible
  results of the calibration, not the filters.
- Sound mode defaults for inputs that are not selected, and per-input
  settings of inputs not yet captured (see above).
- Anything only reachable through the on-screen setup menu or the HTTPS web
  UI, such as network settings, HEOS account, the Pre-amp/
  Audyssey sub-menus not mirrored by a `?` query, input assignment (HDMI/
  digital input per source), and the per-source Input Mode and level
  memory of sources not selected.
- The HTTP `formMainZone_MainZoneXml*` status pages and `description.xml`
  while the web stack returns 403.
- Secrets: the receiver has none here. The serial number is left out on
  purpose (the repo is mirrored publicly).

## Usage

Run on the router (or anywhere that reaches the IoT VLAN):

```sh
cd networking/denon
python3 denonctl.py export            # rewrite denon-x6700h.yaml from the receiver
python3 denonctl.py export -o -       # print instead of writing
python3 denonctl.py export --runtime  # also print runtime state to stderr
python3 denonctl.py drift             # exit 2 on drift
python3 denonctl.py check             # offline: the file parses and maps to known settings
python3 denonctl.py plan              # dry run: commands that would restore the file
```

`--host` (or `DENON_HOST`) overrides `denon.lab.nkontur.com`. `--no-web`
skips the HTTP interface. If the receiver is in standby it reports almost
nothing: `export` refuses, and `drift` prints `SKIPPED` and exits 0.

After an intended change on the receiver (new source name, Audyssey
re-calibration), run `export`, review `git diff`, and commit.

## Drift detection

`denon:drift` in `.gitlab-ci.yml` runs on the pipeline schedule
**"denon: daily AVR settings drift check"** (`DENON_TASK=drift`, daily at
20:45 America/New_York, when the receiver is most likely on). It runs on the
protected runner, which uses the router's host network and so reaches the IoT
VLAN. It is strictly read-only.

| Result | Exit | Effect |
|---|---|---|
| No differences | 0 | Job passes |
| Receiver in standby | 0 | `SKIPPED` line, job passes |
| `DRIFT` (value changed) or `NEW` (setting reported but not in the file) | 2 | Job fails and mails the report to root (`Homelab: Denon AVR settings drift`), the same path as the Aruba and NETGEAR drift checks |
| Receiver unreachable / error | 1 | Same mail |

`UNREAD` (value in the file that the receiver didn't report) and `INFO`
lines (main zone off, input not captured yet) are shown but don't fail the
job. The report is also kept as the `denon-drift.txt` job artifact for 30 days.

`denon:test` (MRs and main, on changes in `networking/denon/`) runs the unit
tests and `denonctl.py check`.

## Apply (designed, disabled)

`apply` exists so the file can be used to restore the receiver (after a
factory reset, a firmware update that lost settings, or a change made by
mistake). It ships disabled:

- `apply` and `plan` without flags only print the plan: for each difference
  between the file and the receiver, the telnet commands that would be sent.
- Sending needs **both** `--send` and the environment variable
  `DENON_APPLY_ENABLE=1`. It is refused under CI (`CI` set), and no CI job
  runs it.
- Speaker and HDMI setup values (`speakers.*`, `hdmi.*`, setup lock) are
  *protected*. They are the results of Audyssey calibration and the setup
  wizard, and changing them by hand de-tunes the room correction, so they
  are refused without `--allow-setup`.
- Only the selected input's `per_input` entry is applied. Other inputs are
  listed as `SKIP` because apply never switches inputs.
- `web.*` differences are listed as `SKIP` (read over HTTP only; no set
  command).
- Every command is built from the query table as `<prefix><value>` (or
  `<prefix><KEY> <value>` for list settings), for example `PSDYNEQ ON`,
  `PSREFLEV 5`, `PSBAS 52`, `CVFL 51`, `SSFUNMPLAY SHIELD`, `SSSODAUX1 DEL`.
  Commands beginning with power, input, volume, mute, menu, system, tuner,
  network, Quick Select or test-tone prefixes are refused when they are built.
- Plan lines marked `[unverified set form]` (the `SS*` setup family, source
  rename and Source Delete) use a set form inferred from the query form that
  hasn't been tested on this firmware. Check them on the receiver once, by
  hand, before relying on them.
- After sending, apply re-reads the receiver and fails if any difference
  remains.

Before the first real use, review the plan, then run
`DENON_APPLY_ENABLE=1 python3 denonctl.py apply --send` from the router with
the receiver on and the intended input selected.

## Home Assistant

The `denonavr` integration (`media_player.denon`, telnet enabled) reads
`http://10.6.128.3:8080/description.xml` at setup. While the receiver's web
stack is wedged, that returns 403 (and the port-80 fallback redirects to
HTTPS), so the integration stays in `setup_retry` and the entity is
unavailable. The fix is to power-cycle the receiver at the mains (unplug for
about 60 s). Standby from the remote isn't enough. The HomeKit accessory
entry *denon* exposes `media_player.denon` (see docs/HOMEASSISTANT.md).
