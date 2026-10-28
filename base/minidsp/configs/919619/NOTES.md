# miniDSP 919619 (USB 1-1.3, ALSA `minidsp_919619`)

Source: original 2x4HD plugin export (plugin XML v1.21, DSP version 100),
saved 2022-11-29, recovered from a settings archive on 2026-10-28.
`preset-0.xml` and `preset-2.xml` are unmodified; `preset-1.xml` was updated
on 2026-10-28 (below). Plugin `settingN.xml` = preset slot N = Home Assistant
preset N-1.

Role (from the config): subwoofer / low-frequency unit, fed from input 1.

| File | HA preset | Summary |
|---|---|---|
| `preset-0.xml` | 0 (normal) | Out 1-2: 8-band room EQ (30-96 Hz), delay 13.2 / 12.5 ms. Out 3: Butterworth 4th-order LPF 60 Hz, delay 2 ms. Out 4: Butterworth 5th-order LPF 30 Hz, delay 2 ms. All gains 0 dB. |
| `preset-1.xml` | 1 (bass boost) | Preset 0 + 10 dB on all four outputs. |
| `preset-2.xml` | 2 (unused) | Near-default: out 1 = in 1, out 2 = in 2, outs 3-4 unrouted, no EQ or delay. |

Out 1/2 PEQ (linked), active bands: PK 30 Hz -8 dB Q3; PK 44 Hz +16 dB Q4;
PK 50 Hz +4 dB Q50; PK 53 Hz +16 dB Q8; PK 61 Hz -10 dB Q10; PK 69 Hz -9 dB Q7;
PK 88 Hz +7 dB Q15; PK 96 Hz +4 dB Q4.

Live meters (2026-10-28) are consistent with preset 0: with no signal,
outputs 3-4 read 13-19 dB below outputs 1-2 because of the low-pass filters
(not a mute).

## History

- 2026-10-28: preset 1 changed to preset 0 + 10 dB on all four outputs
  (outputs 1-2 raised from 0 to +10 dB), applied with minidsp-rs under meter
  checks, so both units now boost every output by the same amount (see
  `../921284/NOTES.md`). `preset-1.xml` here was edited to match.
- Before that, preset 1 added +10 dB on outputs 3-4 only. That version (the
  unmodified 2022 export) is in git history.
