# miniDSP DSP configurations

This directory is for the DSP tuning (filters, crossovers, delays, routing,
gains) of the two miniDSP 2x4HD units on the Z-Wave Pi. The original plugin
exports (2022) were recovered on 2026-10-28 and are stored here unmodified.
Each unit directory has a NOTES.md with a decoded summary; decode any file
with `scripts/minidsp/decode.py <file>`. See `docs/minidsp.md` for the device
map and safety rules.

## The tuning exists only on the devices

- The tuning cannot be read back from a 2x4HD. minidsp-rs and the official
  plugin can write filter settings but cannot download them, so there is no
  way to back up what is loaded now.
- If a unit is reset, replaced or overwritten, its tuning is lost unless the
  original files turn up.
- These files are the only copy outside the devices. When a unit is
  retuned, export the new preset from the plugin and replace the file here.

## Layout

One directory per unit, named by serial (the stable identity; see
`docs/minidsp.md`), and one file per preset slot. The 2x4HD has four presets.
In Home Assistant, preset 0 is "normal" and preset 1 is "bass boost".

```
base/minidsp/configs/
  919619/
    preset-0.xml       # plugin XML for preset slot 0 (normal)
    preset-1.xml       # plugin XML for preset slot 1 (bass boost)
    NOTES.md           # where the file came from, date, speakers, measurements
  921284/
    preset-0.xml
    preset-1.xml
    NOTES.md
```

- Name the directory by serial, never by device index or ALSA card number,
  which follow USB enumeration order.
- Keep the plugin's own XML unmodified. If a converted form (for example
  minidsp-rs JSON) is needed later, add it next to the XML.
- If it is unclear which unit or preset a found file belongs to, put it in
  `unsorted/` with a note instead of guessing.

## Rules for any future apply step

There is no apply step. Writing a configuration to a unit replaces its only
copy of the tuning and can damage speakers if it is wrong. Any automation
added later must:

1. Write on change only. It must compare against the last applied version
   (for example a hash stored on the Pi) and do nothing when the file has not
   changed. It must never write on every deploy, nightly run or reboot.
2. Target units by serial (`/devices/<serial>/...` or `--url` for that
   unit), never by index.
3. Run for the first time with Noah present, the amplifiers at low volume, and
   one unit and one preset at a time.
4. Not be run by CI on merge until steps 1-3 have been done by hand and the
   result listened to.
