# miniDSP 921284 (USB 1-1.2, ALSA `minidsp_921284`)

Source: original 2x4HD plugin export (plugin XML v1.21, DSP version 100),
saved 2022-12-05, recovered from a settings archive on 2026-10-28. Files are
unmodified. Plugin `settingN.xml` = preset slot N = Home Assistant preset N-1.

| File | HA preset | Summary |
|---|---|---|
| `preset-0.xml` | 0 (normal) | All four outputs = input 1 at 0 dB. Delays 11.1 / 11.5 / 5.33 / 4.22 ms. No effective EQ (the advanced biquads are identity coefficients). No crossovers, limiters or FIR. |
| `preset-1.xml` | 1 (bass boost) | +10 dB on all four outputs, no delays, and out 2 also mixes in input 2. |

Live meters (2026-10-28, preset 0) are consistent: signal on input 1 and all
four outputs at similar levels.

Note: bass boost on this unit drops the time alignment (0 ms delays) and adds
input 2 to output 2, so it is more than "preset 0 + 10 dB". Confirm the intent
before relying on or changing it.
