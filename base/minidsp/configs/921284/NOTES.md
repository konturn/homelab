# miniDSP 921284 (USB 1-1.2, ALSA `minidsp_921284`)

Plugin XML v1.21, DSP version 100. `preset-N.xml` = preset slot N+1 in the
plugin = Home Assistant preset N. Two ButtKickers are on outputs 1-2.

## Current (applied 2026-10-28 with minidsp-rs, guarded by meter checks)

| File | HA preset | Summary |
|---|---|---|
| `preset-0.xml` | 0 (normal) | Out 1: Butterworth 4th-order LPF 60 Hz, 2 ms. Out 2: Butterworth 5th-order LPF 30 Hz, 2 ms. Out 3-4: flat, 5.33 / 4.22 ms. All from input 1 at 0 dB. |
| `preset-1.xml` | 1 (bass boost) | Preset 0 + 10 dB on all four outputs. |

The low-pass filters were copied coefficient-for-coefficient from unit
919619 outputs 3-4 (the earlier shaker settings). After the write, the
idle meters of outputs 1-2 dropped below those of outputs 3-4 (about -95 /
-105 dB vs -85 dB), as expected for active low-pass filters.

## History

The original 2022 export (in git history before this change) had no
crossovers, delays 11.1 / 11.5 / 5.33 / 4.22 ms, and a bass-boost preset with
0 ms delays and input 2 also routed to output 2.

The plugin XML here was edited to match what was applied (crossover filter
elements, delay and routing items). Note: minidsp-rs writes only the active
preset, so applying a preset means selecting it first.
