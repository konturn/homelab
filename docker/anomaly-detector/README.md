# anomaly-detector

Deterministic anomaly detection over the homelab's InfluxDB data. The detector
finds anomalies; the OpenClaw agent interprets them (triage of severe alerts,
08:00 digest). See `docker/openclaw/workspace/skills/anomaly-triage/SKILL.md`.

```
Influx (metrics, homeassistant) --aggregate Flux--> detector --+--> Influx `anomaly` measurement (metrics bucket)
                                                               |      -> Grafana dashboard + alert rules
                                                               |         severe -> openclaw-webhook (triage) + email
                                                               +--> MQTT (retained) -> HA sensor.anomaly_<signal>,
                                                               |                       sensor.house_anomalies
                                                               +--> HTTP :8080 /healthz /status /digest /context
                                                                      (OpenClaw: triage + digest, no new secrets)
```

## Files

| file | what |
|---|---|
| `config.yml` | signals: source query, checks, severity, owner, description |
| `suppressions.yml` | known/expected anomalies (signal, series, reason, until) |
| `detector/checks.py` | the check types (pure functions) |
| `detector/engine.py` | live loop: query, check, debounce, suppress, write |
| `detector/backtest.py` | replay the checks over past data |

## Check types

| type | question | key parameters |
|---|---|---|
| `seasonal` | Is this window unusual for this hour of the week? Current window vs the same window 1..N weeks back; robust z = (x - median) / (1.4826 * MAD) | `window` (1h), `weeks` (4), `min_weeks` (3, guard), `z_moderate`/`z_severe` (6/10), `direction`, `rel_scale` (scale floor as a fraction of the median, 0.05), `min_scale`, `min_abs_delta` |
| `rate` | Is the latest bucket-to-bucket change unusual? | `every`, `lookback`, `min_points`, `z_*`, `max_change: {moderate, severe}` |
| `stale` | Has it gone quiet? Age of the newest point, or of the device's own last-seen timestamp (`age_from: value`) | `lookback`, `max_age: {moderate, severe}` |
| `drift` | Is it creeping? Least-squares slope per day over `days` | `days`, `min_points`, `slope: {moderate, severe}`, `min_total_change` |
| `threshold` | Simple bounds over a window | `window`, `agg`, `above`/`below: {moderate, severe}` |

Any check also takes `interval` (how often it runs), `for_cycles` (consecutive
anomalous evaluations before it counts), `max_severity` and
`when_hours: [start, end]` (local time).

**Severity**: `severe` raises the Grafana rule *Anomaly: severe* (pages
through the existing contact points: OpenClaw triage + email). `moderate` does
not page; it appears in the 08:00 digest, on the dashboard and in HA.

## Cost

Every query has explicit bounds and aggregates server-side. A seasonal check
reads N+1 one-hour windows (one `union` request), not N weeks. Seasonal checks
run hourly, drift every 6h, the rest every 5-30 min. A full cycle is about 30
small queries and takes a few seconds; `/status` shows the query counters.

## Add a signal

One entry under `signals:` in `config.yml`:

```yaml
  - name: garage_temperature
    description: Garage air temperature (freeze risk)
    owner: house
    category: physical
    source: {bucket: homeassistant, measurement: "°F", field: value,
             tags: {entity_id: garage_sensor_air_temperature}, state_step: 10m}
    checks:
      - type: threshold
        window: 30m
        below: {moderate: 38, severe: 33}
```

- Home Assistant writes state changes only: set `state_step` so the series is
  forward-filled. Map string states with `value_map: {heating: 1}`; other
  states map to 0.
- Counters (bytes): `derivative: true`.
- Anything else: a raw `flux:` template with `{start}` `{stop}` `{every}`
  placeholders that returns `_time`, `_value` and the `group_by` columns.
- Values `/.../` in `measurement`, `field` or `tags` are Flux regexes.

Then run the backtest (below) and check that the signal flags a handful of
episodes at most. `anomaly-detector:test` parses the file in CI.

## Add a suppression

One entry in `suppressions.yml`, by MR:

```yaml
suppressions:
  - signal: network_devices
    series: "10.6.128.9"     # optional glob, default "*"
    check: threshold         # optional, default "*"
    reason: doorbell unplugged for the winter
    until: 2026-12-31        # required, inclusive, at most a year out
```

Suppressed anomalies are still written (`suppressed=true`) but do not page,
count in HA, or show as active in the digest. Expired entries stop applying
on their own.

## Backtest

```sh
docker exec anomaly-detector python -m detector backtest --days 21
docker exec anomaly-detector python -m detector backtest --days 28 --signal ups_load --json
```

It prints, per signal and check, the number of debounced anomaly episodes
(and per day), with suppressions applied. It reads aggregates in 7-day
chunks; run it by hand, not on a schedule.

## Other commands

```sh
docker exec anomaly-detector python -m detector once          # one forced cycle, dry run
docker exec anomaly-detector python -m detector test-anomaly  # labelled synthetic severe anomaly
docker exec anomaly-detector python -m detector validate
```

`test-anomaly` writes `signal=test_synthetic, category=test` for 8 minutes. The
Grafana rule fires, and OpenClaw triage prefixes its message with `[TEST]`.

## HTTP

| endpoint | access | returns |
|---|---|---|
| `/healthz` | anyone | 200 when the last cycle is recent and the last write succeeded |
| `/status` | `ALLOWED_CLIENTS` | current anomalies, gone-quiet list, suppressions, errors |
| `/digest?hours=24` | `ALLOWED_CLIENTS` | the anomaly measurement aggregated per signal/series over the window |
| `/context` | `ALLOWED_CLIENTS` | weather, indoor climate, presence, recent lock/motion events |

## Credentials

Vault `homelab/docker/anomaly-detector`: `influx_read_token` (read
`metrics` + `homeassistant`), `influx_write_token` (write `metrics` only),
`mqtt_password` (MQTT user `anomaly-detector`, which the deploy adds to the
mosquitto password file).
