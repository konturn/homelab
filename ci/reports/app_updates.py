#!/usr/bin/env python3
"""Weekly read-only check of versions that live in data volumes (runs on the router).

Covers what image updates do not: Nextcloud apps/core, Home Assistant update
entities (integrations, HACS, ...), Grafana plugins, and WordPress core/themes/
plugins versus the pinned image. Nothing is updated. Writes app-updates.{md,json}.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

UA = {"User-Agent": "homelab-update-report/1.0"}


def http_json(url: str, headers: Optional[dict] = None, timeout: int = 20):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------- parsers

_NC_APP = re.compile(r"^Update for (\S+) to version (\S+) is available", re.M)
_NC_CORE = re.compile(r"^(Nextcloud [\d.]+) is available", re.M)


def parse_nextcloud(update_check: str) -> dict:
    return {"core": (_NC_CORE.search(update_check).group(1) if _NC_CORE.search(update_check) else None),
            "apps": [{"app": a, "latest": v} for a, v in _NC_APP.findall(update_check)]}


def parse_ha_updates(states: List[dict]) -> List[dict]:
    out = []
    for s in states:
        if not s.get("entity_id", "").startswith("update.") or s.get("state") != "on":
            continue
        a = s.get("attributes") or {}
        if a.get("skipped_version") and a.get("skipped_version") == a.get("latest_version"):
            continue
        out.append({"entity": s["entity_id"], "title": a.get("title") or a.get("friendly_name") or s["entity_id"],
                    "installed": a.get("installed_version"), "latest": a.get("latest_version")})
    return sorted(out, key=lambda x: x["title"].lower())


_PLUGIN = re.compile(r"^(\S+) @ (\S+)\s*$", re.M)


def parse_grafana_plugins(cli_out: str) -> List[Tuple[str, str]]:
    return _PLUGIN.findall(cli_out)


def parse_css_version(css_head: str) -> Optional[str]:
    m = re.search(r"^\s*\*?\s*Version:\s*(\S+)", css_head, re.M | re.I)
    return m.group(1) if m else None


def image_tag_version(ref: str) -> Optional[str]:
    tag = ref.split("@", 1)[0].rsplit(":", 1)[-1] if ":" in ref.split("@", 1)[0].rsplit("/", 1)[-1] else ""
    m = re.match(r"^v?(\d+(?:\.\d+)+)", tag)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- collectors

def docker_exec(container: str, cmd: List[str], user: Optional[str] = None, timeout: int = 180):
    return common.run(["docker", "exec"] + (["-u", user] if user else []) + [container] + cmd, timeout=timeout)


def check_nextcloud() -> dict:
    r = docker_exec("nextcloud", ["php", "occ", "update:check"], user="www-data", timeout=300)
    if r.returncode not in (0, 1) and not r.stdout:
        return {"error": common.clip(r.stderr or f"rc={r.returncode}", 80)}
    res = parse_nextcloud(r.stdout)
    v = docker_exec("nextcloud", ["php", "occ", "status", "--output=json"], user="www-data")
    try:
        res["installed"] = json.loads(v.stdout).get("versionstring")
    except ValueError:
        pass
    return res


def container_ip(name: str, prefer: str = "internal") -> Optional[str]:
    r = common.run(["docker", "inspect", name])
    try:
        nets = json.loads(r.stdout)[0]["NetworkSettings"]["Networks"]
    except (ValueError, IndexError, KeyError):
        return None
    for k, v in sorted(nets.items(), key=lambda kv: prefer not in kv[0]):
        if v.get("IPAddress"):
            return v["IPAddress"]
    return None


def check_homeassistant(token: Optional[str]) -> dict:
    if not token:
        return {"error": "no Home Assistant token"}
    ip = container_ip("homeassistant")
    base = os.environ.get("HA_URL") or (f"http://{ip}:8123" if ip else "")
    if not base:
        return {"error": "Home Assistant container not found"}
    try:
        states = http_json(f"{base}/api/states", {"Authorization": f"Bearer {token}"})
    except Exception as e:  # noqa: BLE001
        return {"error": f"HA API: {type(e).__name__}"}
    ver = None
    try:
        ver = http_json(f"{base}/api/config", {"Authorization": f"Bearer {token}"}).get("version")
    except Exception:  # noqa: BLE001
        pass
    return {"installed": ver, "updates": parse_ha_updates(states)}


def check_grafana() -> dict:
    r = docker_exec("grafana", ["grafana", "cli", "plugins", "ls"])
    if r.returncode != 0:
        return {"error": common.clip(r.stderr or f"rc={r.returncode}", 80)}
    plugins = []
    for pid, ver in parse_grafana_plugins(r.stdout):
        latest = None
        try:
            latest = http_json(f"https://grafana.com/api/plugins/{urllib.parse.quote(pid)}").get("version")
        except Exception:  # noqa: BLE001 - unknown/private plugin
            pass
        plugins.append({"id": pid, "installed": ver, "latest": latest,
                        "update": bool(latest and common.newer(latest, ver))})
    return {"plugins": plugins}


def _wp_items(kind: str) -> List[Tuple[str, Optional[str]]]:
    base = f"/var/www/html/wp-content/{kind}"
    r = docker_exec("wordpress", ["sh", "-c", f"ls -1 {base}"])
    out = []
    for slug in r.stdout.split():
        if slug.endswith(".php"):
            continue
        f = f"{base}/{slug}/style.css" if kind == "themes" else f"{base}/{slug}/{slug}.php"
        head = docker_exec("wordpress", ["sh", "-c", f"head -c 4096 {f} 2>/dev/null"]).stdout
        out.append((slug, parse_css_version(head)))
    return out


def check_wordpress() -> dict:
    r = docker_exec("wordpress", ["php", "-r", "include '/var/www/html/wp-includes/version.php'; echo $wp_version;"])
    if r.returncode != 0:
        return {"error": common.clip(r.stderr or f"rc={r.returncode}", 80)}
    core = r.stdout.strip()
    ref = common.run(["docker", "inspect", "-f", "{{.Config.Image}}", "wordpress"]).stdout.strip()
    res = {"core": core, "image": ref.split("@", 1)[0], "image_version": image_tag_version(ref),
           "themes": [], "plugins": []}
    try:
        offers = http_json("https://api.wordpress.org/core/version-check/1.7/").get("offers") or []
        res["core_latest"] = offers[0].get("current") if offers else None
    except Exception:  # noqa: BLE001
        res["core_latest"] = None
    for kind, api in (("themes", "themes/info/1.2/?action=theme_information&request[slug]="),
                      ("plugins", "plugins/info/1.2/?action=plugin_information&request[slug]=")):
        for slug, ver in _wp_items(kind):
            latest = None
            try:
                latest = http_json(f"https://api.wordpress.org/{api}{urllib.parse.quote(slug)}").get("version")
            except Exception:  # noqa: BLE001 - not on wordpress.org
                pass
            res[kind].append({"slug": slug, "installed": ver, "latest": latest,
                              "update": bool(ver and latest and common.newer(latest, ver))})
    return res


# --------------------------------------------------------------------------- report

def _major(p: dict) -> bool:
    a, b = re.findall(r"\d+", p.get("installed") or ""), re.findall(r"\d+", p.get("latest") or "")
    return bool(a and b and a[0] != b[0])


def build_report(d: dict) -> Tuple[str, List[str]]:
    parts, lines = [], []
    nc = d.get("nextcloud", {})
    if "error" in nc:
        lines.append(f"Nextcloud: check failed ({nc['error']})")
    else:
        apps = nc.get("apps", [])
        if nc.get("core") or apps:
            parts.append(f"Nextcloud {len(apps)} apps" + (" + core" if nc.get("core") else ""))
        if nc.get("core"):
            lines.append(f"Nextcloud core {nc.get('installed') or '?'}: {nc['core']} available (image update)")
        if apps:
            lines.append(f"Nextcloud apps ({len(apps)}): " +
                         common.join_limited([f"{a['app']} {a['latest']}" for a in apps], 6))
    ha = d.get("homeassistant", {})
    if "error" in ha:
        lines.append(f"Home Assistant: check failed ({ha['error']})")
    elif ha.get("updates"):
        ups = ha["updates"]
        parts.append(f"HA {len(ups)}")
        lines.append(f"Home Assistant updates ({len(ups)}): " + common.join_limited(
            [f"{u['title']} {u['installed']}→{u['latest']}" for u in ups], 5))
    gf = d.get("grafana", {})
    if "error" in gf:
        lines.append(f"Grafana plugins: check failed ({gf['error']})")
    else:
        ups = [p for p in gf.get("plugins", []) if p["update"]]
        if ups:
            parts.append(f"Grafana plugins {len(ups)}")
            lines.append("Grafana plugins: " + common.join_limited(
                [f"{p['id']} {p['installed']}→{p['latest']}" + (" (major)" if _major(p) else "") for p in ups], 5))
    wp = d.get("wordpress", {})
    if "error" in wp:
        lines.append(f"WordPress: check failed ({wp['error']})")
    elif wp:
        drift = wp.get("image_version") and wp.get("core") and wp["core"] != wp["image_version"]
        behind = wp.get("core_latest") and wp.get("core") and common.newer(wp["core_latest"], wp["core"])
        if drift or behind:
            parts.append("WordPress core")
            msg = f"WordPress core {wp['core']}"
            if drift:
                msg += f" (image pin {wp['image_version']}: volume drifted)"
            if behind:
                msg += f"; latest {wp['core_latest']}"
            lines.append(msg)
        ext = [x for x in wp.get("themes", []) + wp.get("plugins", []) if x["update"]]
        if ext:
            parts.append(f"WordPress {len(ext)} themes/plugins")
            lines.append("WordPress themes/plugins: " + common.join_limited(
                [f"{x['slug']} {x['installed']}→{x['latest']}" for x in ext], 5))
    failed = [k for k in ("nextcloud", "homeassistant", "grafana", "wordpress") if "error" in d.get(k, {})]
    summary = ("updates: " + ", ".join(parts)) if parts else "no app-level updates pending"
    if failed:
        summary += f"; check failed: {', '.join(failed)}"
    return summary, lines


def main() -> int:
    argparse.ArgumentParser().parse_known_args()
    secrets = common.read_secrets()
    ha_token = (secrets.get("ha") or {}).get("token")
    data = {}
    for name, fn in (("nextcloud", check_nextcloud), ("homeassistant", lambda: check_homeassistant(ha_token)),
                     ("grafana", check_grafana), ("wordpress", check_wordpress)):
        try:
            data[name] = fn()
        except Exception as e:  # noqa: BLE001 - one failing source must not hide the others
            data[name] = {"error": f"{type(e).__name__}: {common.clip(str(e), 60)}"}
        common.log(f"{name}: {'error' if 'error' in data[name] else 'ok'}")
    summary, lines = build_report(data)
    common.write_report("app-updates", "App-level updates (weekly)", summary, lines, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
