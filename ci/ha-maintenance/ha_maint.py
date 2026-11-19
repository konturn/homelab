#!/usr/bin/env python3
"""Home Assistant maintenance (docs/HOMEASSISTANT.md, "Maintenance job").

Runs on the router (ci/reports/run_remote.py, SCHEDULED_TASK=ha-maintenance,
every 2h 10:00-18:00 America/New_York). Each run:

  1. Inventory: pending update.* entities, Repairs, dead or unresponsive
     Z-Wave nodes, entities unavailable for more than 24h.
  2. Z-Wave nodes that are dead (or alive with every controllable entity
     unavailable for 2h): ping, and on a later run re-interview. At most one
     node action per run, 24h cooldown per node, 2 re-interviews per node per
     week. Never a switch/light command: nothing is turned on or off.
  3. Firmware: AUTO only for mains-powered Z-Wave switches, plugs and dimmers
     (class_of), once the version has been seen for 7 days, inside the
     window, one per day. Every other update not denied gets a tap prompt on
     the JIT bot (Install / Skip); event-router-control records the press in
     DECISIONS and the next run in the window installs it. One install per
     run, verified (version changed, Z-Wave node alive) or reported failed
     (backoff + page through the ha-firmware-update-failed alert).
  4. DENIED, never installed or prompted (classify(), tests): Litter-Robots,
     the SLZB coordinator, UPS, the Solterra, locks / garage / alarm, Home
     Assistant itself, Z-Wave JS and its controller, and container images
     Renovate updates (Plex, Frigate).
  5. Report: reports/updates/ha-maintenance.{md,json} (Morning Digest) and
     one line-protocol point per run in METRICS (telegraf; alerts
     ha-repairs-new -> OpenClaw ops via the event router,
     ha-firmware-update-failed -> page, ha-maintenance-stale).

--dry-run lists what this run would do and changes nothing (no HA calls
that act, no prompts, no state, metrics or report).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import html
import http.client
import json
import os
import re
import secrets
import socket
import sys
import time
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports"))
import common  # noqa: E402  (ci/reports/common.py: report writing)
from hass_ws import HassError, HassWS  # noqa: E402

TZ = ZoneInfo("America/New_York")
WINDOW = (10, 18)                       # local hours [start, end) for any install
SOAK = 7 * 86400                        # auto: version seen this long first
NODE_COOLDOWN = 24 * 3600
PING_TO_REINTERVIEW = 3600              # a ping that did not help, this long ago
REINTERVIEWS_PER_WEEK = 2
UNRESPONSIVE_AFTER = 2 * 3600
UNAVAILABLE_REPORT = 24 * 3600
VERIFY_TIMEOUT = 30 * 60
FAIL_BACKOFF = 7 * 86400                # no auto retry of a failed entity
AUTO_PAUSE = 3 * 86400                  # no auto install at all after any failure
PROMPT_TTL = 72 * 3600
REPROMPT_AFTER = 7 * 86400              # an unanswered prompt is re-sent at most weekly
PROMPTS_PER_RUN = 3

STATE_DIR = os.environ.get("HA_MAINT_DIR", "/var/lib/ha-maintenance")
STATE = os.path.join(STATE_DIR, "state.json")
DECISIONS = os.path.join(STATE_DIR, "decisions.jsonl")      # written by event-router-control
METRICS = os.path.join(STATE_DIR, "ha_maintenance.influx")  # read by telegraf
JIT_SOCKET = os.environ.get("JIT_ACTIONS_SOCKET",
                            "/persistent_data/application/jit-approval-svc/actions/actions.sock")
PROMPT_ID = re.compile(r"^haup-[0-9a-f]{12}$")

# --- policy ------------------------------------------------------------------------------
# Integrations whose update entities this job never touches.
DENY_PLATFORMS = {
    "litterrobot": "Litter-Robot (stays on ESP 1.1.75)",
    "whiskerless": "Litter-Robot (stays on ESP 1.1.75)",
    "smlight": "SLZB Zigbee coordinator",
    "nut": "UPS", "apcupsd": "UPS",
    "subaru": "Solterra", "mysubaru": "Solterra",
    "hassio": "Home Assistant itself (Renovate)",
    "plex": "container image (Renovate)",
    "frigate": "container image (Renovate)",
}
# Matched against entity_id, device name, manufacturer and model (lower case).
DENY_NAMES = [
    (re.compile(r"whisker|litter[-_ ]?robot"), "Litter-Robot (stays on ESP 1.1.75)"),
    (re.compile(r"slzb|smlight"), "SLZB Zigbee coordinator"),
    (re.compile(r"\bups\b|smart-ups|\bapc\b"), "UPS"),
    (re.compile(r"solterra|subaru"), "Solterra"),
    (re.compile(r"garage"), "garage"),
    (re.compile(r"\block\b|deadbolt|keymaster"), "lock"),
    (re.compile(r"alarm|security panel"), "alarm"),
    (re.compile(r"home[ _]assistant[ _](core|supervisor|operating)|home_assistant_(core|supervisor|operating)"),
     "Home Assistant itself (Renovate)"),
    (re.compile(r"z-?wave[ _-]?js|zwave_js_ui|z-?wave controller|\bzst\d|z-stick"), "Z-Wave JS / controller"),
]
# A device with any of these entities is a lock, alarm or garage, whatever its name.
DENY_DOMAINS = {"lock": "lock", "alarm_control_panel": "alarm"}
DENY_COVER_CLASSES = {"garage", "gate", "door"}
# Controllable domains of a "switch, plug or dimmer".
SAFE_DOMAINS = {"switch", "light"}
CONTROL_DOMAINS = {"switch", "light", "fan", "cover", "climate", "lock", "siren", "valve",
                   "media_player", "vacuum", "humidifier", "water_heater", "alarm_control_panel"}
# Loads that must not lose power even for a reboot: never auto, always a tap.
NEVER_AUTO_NAMES = re.compile(r"fridge|freezer|refrigerator|sump|aquarium|server|router|modem|network|ups")


def classify(u: dict) -> Tuple[str, str]:
    """(deny | auto | tap, reason) for one pending update.

    u: entity_id, platform, name, manufacturer, model, domains (controllable
    domains on the device), cover_classes, battery (bool)."""
    plat = (u.get("platform") or "").lower()
    if plat in DENY_PLATFORMS:
        return "deny", DENY_PLATFORMS[plat]
    text = " ".join(str(u.get(k) or "") for k in ("entity_id", "name", "manufacturer", "model")).lower()
    for rx, why in DENY_NAMES:
        if rx.search(text):
            return "deny", why
    domains = set(u.get("domains") or ())
    for d, why in DENY_DOMAINS.items():
        if d in domains:
            return "deny", why
    if "cover" in domains and set(u.get("cover_classes") or ()) & DENY_COVER_CLASSES:
        return "deny", "garage"
    if plat != "zwave_js":
        return "tap", "not a Z-Wave switch/plug/dimmer"
    if u.get("battery"):
        return "tap", "battery-powered"
    if not domains or not domains <= SAFE_DOMAINS:
        return "tap", "not a switch/plug/dimmer"
    if NEVER_AUTO_NAMES.search(text):
        return "tap", "load must not lose power"
    return "auto", "mains-powered Z-Wave switch/plug/dimmer"


def in_window(now: float) -> bool:
    h = dt.datetime.fromtimestamp(now, TZ).hour
    return WINDOW[0] <= h < WINDOW[1]


def local_day(now: float) -> str:
    return dt.datetime.fromtimestamp(now, TZ).date().isoformat()


def pick_install(updates: List[dict], st: dict, now: float) -> Optional[Tuple[dict, str]]:
    """The one install this run may start, or None: an approved tap first, then
    one auto (soaked, not failed recently, none today, not paused)."""
    if not in_window(now):
        return None
    for u in sorted(updates, key=lambda u: u["entity_id"]):
        a = st["approved"].get(u["entity_id"])
        if u["decision"] != "deny" and a and a.get("version") == u["latest"]:
            return u, "approved"
    if st.get("auto_paused_until", 0) > now or st.get("last_auto_day") == local_day(now):
        return None
    for u in sorted(updates, key=lambda u: st["seen"].get(u["entity_id"], {}).get("since", now)):
        if u["decision"] != "auto":
            continue
        seen = st["seen"].get(u["entity_id"], {})
        if seen.get("version") != u["latest"] or now - seen.get("since", now) < SOAK:
            continue
        if now - st["failed"].get(u["entity_id"], 0) < FAIL_BACKOFF:
            continue
        return u, "auto"
    return None


def pick_node_action(nodes: List[dict], st: dict, now: float) -> Optional[Tuple[dict, str]]:
    """At most one (node, ping | reinterview) per run for dead/unresponsive nodes."""
    for n in sorted(nodes, key=lambda n: n["device_id"]):
        h = st["nodes"].setdefault(n["device_id"], {"history": []})
        hist = [x for x in h["history"] if now - x["ts"] < 7 * 86400]
        last = hist[-1] if hist else None
        if last and last["kind"] == "ping" and now - last["ts"] >= PING_TO_REINTERVIEW and \
                sum(1 for x in hist if x["kind"] == "reinterview") < REINTERVIEWS_PER_WEEK and \
                not any(x["kind"] == "reinterview" and now - x["ts"] < NODE_COOLDOWN for x in hist):
            return n, "reinterview"
        if not any(x["kind"] == "ping" and now - x["ts"] < NODE_COOLDOWN for x in hist):
            return n, "ping"
    return None


def tap_groups(updates: List[dict], st: dict, now: float) -> List[List[dict]]:
    """Tap updates that still need a prompt, grouped by (platform, model, version)."""
    groups: Dict[tuple, List[dict]] = {}
    for u in sorted(updates, key=lambda u: u["entity_id"]):
        if u["decision"] != "tap" or u["in_progress"] or st["skipped"].get(u["entity_id"]) == u["latest"] \
                or st["approved"].get(u["entity_id"], {}).get("version") == u["latest"]:
            continue
        sent = [p["at"] for p in st["prompts"].values()
                if u["entity_id"] in p["entity_ids"] and p["version"] == u["latest"]]
        if sent and now - max(sent) < REPROMPT_AFTER:
            continue
        groups.setdefault((u["platform"], u.get("model") or u["entity_id"], u["latest"]), []).append(u)
    return list(groups.values())


# --- HA inventory ------------------------------------------------------------------------
def ha_url() -> str:
    url = os.environ.get("HA_URL")
    if url:
        return url
    r = common.run(["docker", "inspect", "homeassistant"])
    nets = json.loads(r.stdout)[0]["NetworkSettings"]["Networks"]
    for name, v in sorted(nets.items(), key=lambda kv: "internal" not in kv[0]):
        if v.get("IPAddress"):
            return f"http://{v['IPAddress']}:8123"
    raise SystemExit("no address for the homeassistant container")


def ts(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def inventory(ha: HassWS, now: float) -> dict:
    states = {s["entity_id"]: s for s in ha.call("get_states")}
    ents = ha.call("config/entity_registry/list")
    devs = {d["id"]: d for d in ha.call("config/device_registry/list")}
    by_dev: Dict[str, List[dict]] = {}
    for e in ents:
        if e.get("device_id"):
            by_dev.setdefault(e["device_id"], []).append(e)

    def dev_info(did):
        d = devs.get(did) or {}
        es = [e for e in by_dev.get(did, []) if not e.get("disabled_by")]
        domains = {e["entity_id"].split(".")[0] for e in es} & CONTROL_DOMAINS
        covers = {(states.get(e["entity_id"], {}).get("attributes") or {}).get("device_class")
                  for e in es if e["entity_id"].startswith("cover.")}
        battery = any(e["entity_id"].startswith("sensor.") and
                      ((states.get(e["entity_id"], {}).get("attributes") or {}).get("device_class") == "battery"
                       or e.get("original_device_class") == "battery") for e in es)
        return {"name": d.get("name_by_user") or d.get("name"), "manufacturer": d.get("manufacturer"),
                "model": d.get("model"), "domains": sorted(domains), "cover_classes": sorted(c for c in covers if c),
                "battery": battery}

    emap = {e["entity_id"]: e for e in ents}
    updates = []
    for eid, s in states.items():
        if not eid.startswith("update.") or s["state"] != "on":
            continue
        a, e = s.get("attributes") or {}, emap.get(eid, {})
        u = {"entity_id": eid, "platform": e.get("platform"), "device_id": e.get("device_id"),
             "installed": a.get("installed_version"), "latest": a.get("latest_version"),
             "title": a.get("title") or a.get("friendly_name"), "release_url": a.get("release_url"),
             "in_progress": bool(a.get("in_progress")), "skipped": a.get("skipped_version")}
        u.update(dev_info(e.get("device_id")) if e.get("device_id") else
                 {"name": a.get("friendly_name"), "manufacturer": None, "model": None, "domains": [],
                  "cover_classes": [], "battery": False})
        u["decision"], u["reason"] = classify(u)
        updates.append(u)

    nodes = []
    for e in ents:
        if e.get("platform") != "zwave_js" or not e["entity_id"].endswith("_node_status") or e.get("disabled_by"):
            continue
        s = states.get(e["entity_id"]) or {}
        did = e.get("device_id")
        controls = [states[x["entity_id"]] for x in by_dev.get(did, [])
                    if x["entity_id"].split(".")[0] in CONTROL_DOMAINS and x["entity_id"] in states]
        dead = s.get("state") == "dead"
        unresponsive = (not dead and controls and
                        all(c["state"] == "unavailable" and now - ts(c["last_changed"]) > UNRESPONSIVE_AFTER
                            for c in controls))
        if dead or unresponsive:
            info = dev_info(did)
            nodes.append({"device_id": did, "name": info["name"], "model": info["model"],
                          "status": "dead" if dead else "unresponsive",
                          "ping_entity": next((x["entity_id"] for x in by_dev.get(did, [])
                                               if x["entity_id"].startswith("button.") and x["entity_id"].endswith("_ping")), None)})

    issues = [i for i in (ha.call("repairs/list_issues") or {}).get("issues", [])
              if not i.get("dismissed_version") and not i.get("ignored")]
    unavailable = sorted(
        ({"entity_id": eid, "name": (s.get("attributes") or {}).get("friendly_name"), "since": s["last_changed"][:16]}
         for eid, s in states.items()
         if s["state"] == "unavailable" and now - ts(s["last_changed"]) > UNAVAILABLE_REPORT
         and not eid.startswith("update.")),
        key=lambda x: x["since"])
    return {"updates": updates, "nodes": nodes, "issues": issues, "unavailable": unavailable}


# --- actions -----------------------------------------------------------------------------
def jit(method: str, path: str, body=None) -> int:
    c = http.client.HTTPConnection("localhost", timeout=15)
    c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.sock.settimeout(15)
    c.sock.connect(JIT_SOCKET)
    try:
        c.request(method, path, body=json.dumps(body) if body is not None else None,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        return r.status
    finally:
        c.close()


def prompt_text(u: dict, count: int = 1) -> str:
    what = u.get("name") or u["entity_id"]
    if count > 1:
        what = f"{count} devices ({u.get('model') or what})"
    lines = [f"<b>Home Assistant update</b>: {html.escape(str(what))}",
             f"{html.escape(str(u.get('installed')))} → <b>{html.escape(str(u.get('latest')))}</b>"
             f" ({html.escape(str(u.get('platform')))})"]
    if u.get("release_url"):
        lines.append(html.escape(str(u["release_url"])))
    lines.append("Install runs during 10:00–18:00, one update at a time, and is verified afterwards.")
    return "\n".join(lines)


def read_decisions() -> List[dict]:
    """Presses event-router-control recorded since the last run (consumed here)."""
    try:
        f = open(DECISIONS, "r+")
    except FileNotFoundError:
        return []
    with f:
        fcntl.flock(f, fcntl.LOCK_EX)
        out = []
        for line in f.read().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if PROMPT_ID.match(str(d.get("id"))) and d.get("action") in ("install", "skip"):
                out.append(d)
        f.seek(0)
        f.truncate()
    return out


def apply_decisions(st: dict, decisions: List[dict], now: float) -> List[str]:
    log = []
    for d in decisions:
        p = st["prompts"].pop(d["id"], None)
        if not p:
            continue
        for eid in p["entity_ids"]:
            if d["action"] == "install":
                st["approved"][eid] = {"version": p["version"], "at": now}
            else:
                st["skipped"][eid] = p["version"]
        log.append(f"{'approved' if d['action'] == 'install' else 'skipped'} "
                   f"{common.join_limited(p['entity_ids'])} {p['version']}")
    return log


def wait_installed(ha: HassWS, u: dict, deadline: float) -> Tuple[bool, str]:
    while time.time() < deadline:
        time.sleep(30)
        try:
            s = {x["entity_id"]: x for x in ha.call("get_states")}
        except (HassError, OSError) as e:
            return False, f"lost Home Assistant while waiting: {e}"
        a = (s.get(u["entity_id"]) or {}).get("attributes") or {}
        if a.get("in_progress"):
            continue
        if a.get("installed_version") == u["latest"] or (
                (s.get(u["entity_id"]) or {}).get("state") == "off" and a.get("installed_version") != u["installed"]):
            return True, "installed"
    return False, f"not at {u['latest']} after {VERIFY_TIMEOUT // 60} min"


def node_alive(ha: HassWS, device_id: str) -> bool:
    ents = [e for e in ha.call("config/entity_registry/list") if e.get("device_id") == device_id
            and e["entity_id"].endswith("_node_status")]
    if not ents:
        return True
    s = {x["entity_id"]: x for x in ha.call("get_states")}
    return all((s.get(e["entity_id"]) or {}).get("state") in ("alive", "asleep", "awake") for e in ents)


# --- state, metrics ----------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE) as f:
            st = json.load(f)
    except (FileNotFoundError, ValueError):
        st = {}
    for k in ("seen", "approved", "skipped", "prompts", "failed", "nodes", "issues"):
        st.setdefault(k, {})
    return st


def save_state(st: dict) -> None:
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def write_metric(fields: Dict[str, int], now: float) -> None:
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    line = "ha_maintenance " + ",".join(f"{k}={int(v)}i" for k, v in sorted(fields.items())) + \
           f" {int(now * 1e9)}\n"
    try:
        with open(METRICS) as f:
            lines = f.readlines()[-199:]
    except FileNotFoundError:
        lines = []
    tmp = METRICS + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(lines + [line])
    os.chmod(tmp, 0o644)
    os.replace(tmp, METRICS)


def track(st: dict, inv: dict, now: float) -> int:
    """Remember when each pending version and each repair was first seen;
    returns the number of repairs first seen by this run (so the alert fires
    once per repair, not on every run for a day)."""
    pending = {u["entity_id"]: u["latest"] for u in inv["updates"]}
    for eid, ver in pending.items():
        if st["seen"].get(eid, {}).get("version") != ver:
            st["seen"][eid] = {"version": ver, "since": now}
    for d in ("seen", "approved", "skipped"):
        for eid in [e for e in st[d] if e not in pending]:
            del st[d][eid]
    cur = {f"{i['domain']}/{i['issue_id']}" for i in inv["issues"]}
    new = [k for k in cur if k not in st["issues"]]
    for k in new:
        st["issues"][k] = now
    for k in [k for k in st["issues"] if k not in cur]:
        del st["issues"][k]
    st["prompts"] = {k: v for k, v in st["prompts"].items() if now - v["at"] < PROMPT_TTL + 86400}
    return len(new)


# --- main --------------------------------------------------------------------------------
def run(ha: HassWS, st: dict, now: float, dry: bool) -> Tuple[dict, List[str], Dict[str, int]]:
    inv = inventory(ha, now)
    actions: List[str] = []
    m = {"installs_ok": 0, "installs_failed": 0, "node_actions": 0, "prompts_sent": 0}
    repairs_new = track(st, inv, now)     # in a dry run the state is never saved
    if not dry:
        actions += apply_decisions(st, read_decisions(), now)

    # Z-Wave: one node action.
    pick = pick_node_action(inv["nodes"], st, now)
    if pick:
        n, kind = pick
        actions.append(f"{'would ' if dry else ''}{kind} Z-Wave node {n['name']} ({n['status']})")
        if not dry:
            try:
                if kind == "ping":
                    ha.call("call_service", domain="zwave_js", service="ping",
                            target={"device_id": n["device_id"]})
                else:
                    ha.call("zwave_js/refresh_node_info", device_id=n["device_id"])
                st["nodes"][n["device_id"]]["history"].append({"kind": kind, "ts": now})
                m["node_actions"] += 1
            except HassError as e:
                actions.append(f"{kind} failed: {e}")

    # Taps for everything not denied and not auto, once per version (weekly re-send).
    # Identical updates (same integration, model and version, e.g. a batch of
    # bulbs) share one prompt; at most PROMPTS_PER_RUN new prompts per run.
    for group in tap_groups(inv["updates"], st, now)[:PROMPTS_PER_RUN]:
        first = group[0]
        label = first.get("name") or first["entity_id"]
        if len(group) > 1:
            label = f"{len(group)}x {first.get('model') or label}"
        actions.append(f"{'would ask' if dry else 'asked'} to install {label} {first['latest']} ({first['reason']})")
        if not dry:
            pid = "haup-" + secrets.token_hex(6)
            try:
                code = jit("POST", "/v1/prompts", {"id": pid, "text": prompt_text(first, len(group)),
                                                  "ttl_seconds": PROMPT_TTL,
                                                  "buttons": [{"action": "install", "label": "Install"},
                                                              {"action": "skip", "label": "Skip"}]})
            except OSError as e:
                code = f"error {e}"
            if code in (200, 201):
                st["prompts"][pid] = {"entity_ids": [u["entity_id"] for u in group],
                                      "version": first["latest"], "at": now}
                m["prompts_sent"] += 1
            else:
                actions.append(f"prompt failed ({code})")

    # One install.
    choice = pick_install(inv["updates"], st, now)
    if choice:
        u, why = choice
        actions.append(f"{'would install' if dry else 'installing'} {u['entity_id']} "
                       f"{u['installed']} -> {u['latest']} ({why})")
        if not dry:
            if why == "auto":
                st["last_auto_day"] = local_day(now)
            st["approved"].pop(u["entity_id"], None)
            save_state(st)          # an interrupted run never repeats this install
            try:
                ha.call("call_service", domain="update", service="install",
                        target={"entity_id": u["entity_id"]})
                ok, detail = wait_installed(ha, u, time.time() + VERIFY_TIMEOUT)
                if ok and u.get("device_id") and u["platform"] == "zwave_js" and not node_alive(ha, u["device_id"]):
                    ok, detail = False, "installed, but the Z-Wave node is not alive"
            except (HassError, OSError) as e:
                ok, detail = False, str(e)
            actions.append(f"{u['entity_id']}: {detail}")
            if ok:
                m["installs_ok"] += 1
            else:
                m["installs_failed"] += 1
                st["failed"][u["entity_id"]] = now
                st["auto_paused_until"] = now + AUTO_PAUSE

    counts = {
        "updates_pending": len(inv["updates"]),
        "updates_auto": sum(u["decision"] == "auto" for u in inv["updates"]),
        "updates_tap": sum(u["decision"] == "tap" for u in inv["updates"]),
        "updates_denied": sum(u["decision"] == "deny" for u in inv["updates"]),
        "tap_waiting": len(st["prompts"]),
        "repairs_open": len(inv["issues"]),
        "repairs_new": repairs_new,
        "zwave_dead": sum(n["status"] == "dead" for n in inv["nodes"]),
        "zwave_unresponsive": sum(n["status"] == "unresponsive" for n in inv["nodes"]),
        "unavailable_24h": len(inv["unavailable"]),
        "collect_ok": 1,
    }
    counts.update(m)
    return inv, actions, counts


def report(inv: dict, actions: List[str], c: Dict[str, int]) -> None:
    summary = (f"{c['updates_pending']} updates ({c['updates_auto']} auto, {c['updates_tap']} tap, "
               f"{c['updates_denied']} held), {c['repairs_open']} repairs, "
               f"{c['zwave_dead'] + c['zwave_unresponsive']} Z-Wave nodes down, {c['unavailable_24h']} unavailable >24h")
    lines = list(actions)
    lines += [f"repair: {i['domain']} {i.get('translation_key') or i['issue_id']} ({i['severity']})" for i in inv["issues"]]
    lines += [f"Z-Wave {n['status']}: {n['name']}" for n in inv["nodes"]]
    lines += [f"update {u['decision']}: {u.get('name') or u['entity_id']} {u['installed']} -> {u['latest']} ({u['reason']})"
              for u in sorted(inv["updates"], key=lambda u: (u["decision"] != "auto", u["decision"] != "tap", u["entity_id"]))]
    lines += [f"unavailable since {x['since']}: {x['name'] or x['entity_id']}" for x in inv["unavailable"]]
    common.write_report("ha-maintenance", "Home Assistant maintenance", summary, lines,
                        {"actions": actions, "counts": c, **inv})


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    token = (common.read_secrets().get("ha") or {}).get("token") or os.environ.get("HA_TOKEN")
    if not token:
        print("no Home Assistant token (secret ha=docker/homeassistant)", file=sys.stderr)
        return 2
    now = time.time()
    st = load_state()
    try:
        with HassWS(ha_url(), token) as ha:
            inv, actions, counts = run(ha, st, now, a.dry_run)
    except (OSError, HassError, PermissionError) as e:
        print(f"ha-maintenance: {type(e).__name__}: {e}", file=sys.stderr)
        if not a.dry_run:
            write_metric({"collect_ok": 0}, now)
        return 1
    for line in actions:
        print(line)
    print(json.dumps(counts, sort_keys=True))
    if a.dry_run:
        for u in inv["updates"]:
            print(f"  {u['decision']:5} {u['entity_id']} {u['installed']} -> {u['latest']}: {u['reason']}")
        for n in inv["nodes"]:
            print(f"  node {n['status']}: {n['name']} ({n['model']})")
        for i in inv["issues"]:
            print(f"  repair: {i['domain']} {i.get('translation_key') or i['issue_id']}")
        print(f"  unavailable >24h: {len(inv['unavailable'])}")
        return 0
    save_state(st)
    write_metric(counts, now)
    report(inv, actions, counts)
    return 1 if counts["installs_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
