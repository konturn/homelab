---
name: anomaly-triage
description: Triage anomaly-detector alerts (Grafana "Anomaly: severe"), write the 08:00 anomaly digest, and turn "that's expected" into a suppression MR. Use for any alert with label source=anomaly-detector.
---

# Anomaly triage

The anomaly detector (container `anomaly-detector`, code and config in
`docker/anomaly-detector/` of the homelab repo) finds anomalies with fixed
rules. Your job is interpretation: add context, decide whether Noah needs to
know, and keep messages short. Repo-managed and read-only here; change it by
MR.

Deployed root-owned from `docker/openclaw/workspace/skills/anomaly-triage/` in
the homelab repo.

## Data sources (no credentials needed)

From the gateway container:

```bash
curl -s http://anomaly-detector:8080/status | jq .         # current anomalies, gone_quiet, suppressions, errors
curl -s "http://anomaly-detector:8080/digest?hours=24" | jq .   # last 24h of the anomaly measurement, aggregated
curl -s http://anomaly-detector:8080/context | jq .        # weather station, indoor climate, presence, recent lock/motion/garage events
```

`/status` fields: `anomalies[]` (unsuppressed, non-ok), `gone_quiet[]`,
`suppressed[]`, `pending[]` (anomalous but not yet debounced), `errors[]`,
`signals[]` (descriptions). Each anomaly has `signal`, `check` (seasonal, rate,
stale, drift, threshold), `series`, `label`, `severity`, `value`, `baseline`,
`score` and `detail` (human-readable).

Recent deploys and pipelines (GitLab, project 4):

```bash
source /home/node/.openclaw/workspace/tools/jit-lib.sh 2>/dev/null
curl -s -H "PRIVATE-TOKEN: $(gitlab_token)" \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4/deployments?order_by=created_at&sort=desc&per_page=5" \
  | jq -r '.[] | "\(.created_at) \(.environment.name) \(.status) \(.deployable.commit.title)"'
```

For deeper digging (Loki logs, Influx), use the JIT tier-1 tokens as in
ALERTS.md (`jit_grafana_token`, `jit_influxdb_token`). Do **not** request a
Home Assistant token for triage: it is tier 2, so it pings Noah for approval,
and `/context` already covers what triage needs.

## Severe alert triage (Grafana webhook)

You get a message like
`Anomaly: severe (critical) [firing]: Anomaly (physical): basement_humidity - [threshold]`.

1. **Status.** If the alert is `[resolved]`, reply `NO_REPLY` unless it was
   never reported and still matters.
2. **Test.** If the summary starts with `[TEST]`, or the signal is
   `test_synthetic`, or category is `test`: this is an end-to-end test. Still
   do steps 3-4 (to prove they work), then send exactly one message starting
   with `[TEST]`, e.g.
   `[TEST] Anomaly pipeline OK: test_synthetic severe received, /status reachable (N active), context OK.`
3. **Gather context** (each is one call):
   - `/status`: is the anomaly still active? What else is anomalous right now
     (one root cause often trips several signals)?
   - recent deploys (above): a deploy in the last 2h explains container
     restarts, iowait spikes, short telegraf/ping gaps;
   - `/context`: outdoor temperature, wind and rain (explains indoor
     temperature, humidity and furnace runtime), presence (`person.noah`:
     `home` vs away), recent lock/motion events.
4. **Decide.** Notify only if it is **actionable**: Noah should do something
   now or soon. Rough guide:
   - Usually actionable: UPS on battery or low charge; basement humidity or
     temperature out of bounds; indoor temperature falling fast or out of
     bounds; a telegraf host or a mains-powered device gone quiet with no
     deploy to explain it; SMART pending or reallocated sectors growing; a
     filesystem filling fast.
   - Usually not: anything explained by a deploy or a reboot in the last 2h,
     weather-driven changes within reason, a device that recovered before you
     looked (`/status` no longer lists it), lock activity at night while Noah
     is home.
5. **Message** (only if actionable): one line of why, plus one suggested
   action. No headers, no lists, at most ~300 characters:
   `⚠️ Basement humidity 78% (normally ~55% at this hour), rising for 3h; outdoor dry. Check the sump pit and the dehumidifier.`
   Then reply `NO_REPLY` if you already sent it with the message tool, or
   make that line your entire final reply. Never both.
6. **Not actionable:** your entire final reply is `NO_REPLY` (that exact
   token, nothing before or after it). Append one line to
   `memory/anomaly-triage.log` with the time, the signal and why you stayed
   quiet.

## Morning digest (cron "anomaly-digest", 08:00)

1. `curl -s "http://anomaly-detector:8080/digest?hours=24"`.
   If it fails, the digest is one line: `Anomaly digest: detector unreachable (<error>)`.
2. Build one short Telegram message (your final reply is delivered as-is):
   - first line: `🏠 Anomalies (24h): N severe, M moderate` or
     `🏠 Anomalies (24h): all quiet`;
   - one line per item in `items` where `suppressed` is false, worst first, at
     most 8: `label/signal: detail-ish summary (≈approx_minutes min, last at HH:MM)`.
     Merge items that obviously share a cause (e.g. many Z-Wave sensors at
     once = one Z-Wave outage);
   - `Gone quiet now:` with `gone_quiet` labels, if any;
   - `Detector errors:` if `errors` is non-empty or `healthy` is false;
   - `Suppressions expiring this week:` from `suppressions_expiring_7d`, if any.
3. Skip items with `signal: test_synthetic`.
4. No headers beyond that, no advice unless something is clearly actionable.
   All times America/New_York.

## Feedback: "that's expected" -> suppression MR

When Noah replies that an anomaly is expected or normal ("that's fine", "I
was running a backup", "the doorbell is unplugged"):

1. Ask for an expiry only if he did not give one. Otherwise default to 14 days
   (maintenance, travel) or 90 days (a device knowingly offline). Never more
   than a year; CI rejects that.
2. Worktree from the homelab clone (never edit `workspace/homelab` itself):
   ```bash
   cd ~/.openclaw/workspace/homelab && git fetch origin
   git worktree add -b anomaly-suppress/<signal>-<yyyymmdd> ../homelab-suppress-<signal> origin/main
   ```
3. Append to `docker/anomaly-detector/suppressions.yml`:
   ```yaml
     - signal: <signal>            # exact name from /status
       series: "<series>"          # from /status; omit to cover all series
       check: <check>              # optional
       reason: <neutral, one line> # e.g. "planned backup window"
       until: YYYY-MM-DD
   ```
   The repo is **public**: put no names, locations, travel plans, addresses or
   security details in `reason`, branch or commit message. Put context in the
   MR description instead (not mirrored).
4. Commit `anomaly: suppress <signal> until <date>`, push, and open an MR to
   `main` from inside the worktree (`glab mr create`, label
   `anomaly-suppression`). The `anomaly-detector:test` job validates the file.
5. **Do not merge or approve it.** `docker/anomaly-detector/` is owned by @root
   in CODEOWNERS, so Noah's approval is required. Tell him in one line:
   `Suppression MR !NNN: <signal> until <date>. Approve to apply.`
6. After merge, the deploy restarts the detector, and `/status` lists the entry
   under `suppressions.active`.

If Noah says a threshold is wrong rather than a one-off ("humidity 66% is
fine"), propose a `config.yml` change in the same way instead of a
suppression.
