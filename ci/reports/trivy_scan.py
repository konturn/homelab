#!/usr/bin/env python3
"""Daily Trivy scan of the images of running containers (runs on the router).

Only HIGH/CRITICAL findings that have a fixed version are counted. The Trivy
vulnerability DB is cached under TRIVY_CACHE and refreshed once per run.
Writes reports/updates/cves.{md,json}.

Each image is scanned once at every severity, fixed or not; the report filters
afterwards. The full result is also exported, reduced to one compact row per
finding, to SCAN_EXPORT_DIR (root-only, on the router), so other local tools
can reuse this scan instead of scanning every image again. Set
SCAN_EXPORT_DIR="" to disable the export.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
import tempfile
from collections import defaultdict
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

TRIVY_IMAGE = os.environ.get(
    "TRIVY_IMAGE",
    "aquasec/trivy:0.76.0@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222")
TRIVY_CACHE = os.environ.get("TRIVY_CACHE", "/persistent_data/application/trivy/cache")
SCAN_EXPORT_DIR = os.environ.get("SCAN_EXPORT_DIR", "/var/lib/trivy-reports/images")
# Trivy holds an exclusive lock on its cache, so scans run one at a time.
PARALLEL = int(os.environ.get("TRIVY_PARALLEL", "1"))
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1}


def trivy(args: List[str], timeout: int) -> "common.subprocess.CompletedProcess":
    # --log-driver none: the JSON report is read from the attached stdout, never
    # from `docker logs`. With the default json-file driver promtail shipped every
    # report to Loki: 1.33M lines from one run on 2026-10-19, about 2x GitLab's
    # daily volume, under ~36 one-off auto-generated container names.
    return common.run(["docker", "run", "--rm", "--log-driver", "none",
                       "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
                       "-v", f"{TRIVY_CACHE}:/root/.cache/trivy", TRIVY_IMAGE] + args, timeout=timeout)


def is_ci_job(c: dict) -> bool:
    """Short-lived GitLab CI job containers are not part of the running stack."""
    labels = (c.get("Config") or {}).get("Labels") or {}
    return (c.get("Name", "").lstrip("/").startswith("runner-")
            or any(k.startswith("com.gitlab.gitlab-runner") for k in labels))


def running_images() -> Dict[str, dict]:
    """image id -> {ref, services, containers}"""
    ids = common.run(["docker", "ps", "-q"]).stdout.split()
    if not ids:
        return {}
    info = json.loads(common.run(["docker", "inspect"] + ids, timeout=60).stdout or "[]")
    out: Dict[str, dict] = {}
    for c in info:
        if is_ci_job(c):
            continue
        img = c.get("Image")
        ref = (c.get("Config") or {}).get("Image") or img
        labels = (c.get("Config") or {}).get("Labels") or {}
        e = out.setdefault(img, {"ref": ref, "services": set(), "containers": set()})
        e["containers"].add(c.get("Name", "").lstrip("/"))
        e["services"].add(labels.get("com.docker.compose.service") or c.get("Name", "").lstrip("/"))
    return out


def display_ref(ref: str) -> str:
    return ref.split("@", 1)[0]


def repo_of(ref: str) -> str:
    """Image repository without tag/digest: 'nginx:1.2@sha256:..' -> 'nginx'."""
    name = ref.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    return name


def summarise_scan(result: dict) -> dict:
    """Reduce Trivy JSON to counts and a few package hints (fixable findings only)."""
    vulns = {}
    for r in result.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            if not v.get("FixedVersion") or v.get("Severity") not in SEV_ORDER:
                continue
            key = (v.get("VulnerabilityID"), v.get("PkgName"))
            vulns[key] = v
    ids_by_sev: Dict[str, set] = defaultdict(set)
    pkgs: Dict[str, dict] = {}
    for v in vulns.values():
        ids_by_sev[v["Severity"]].add(v["VulnerabilityID"])
        p = pkgs.setdefault(v["PkgName"], {"pkg": v["PkgName"], "worst": v["Severity"], "count": 0,
                                           "installed": v.get("InstalledVersion", ""), "fixed": set()})
        p["count"] += 1
        if SEV_ORDER[v["Severity"]] < SEV_ORDER[p["worst"]]:
            p["worst"] = v["Severity"]
        p["fixed"].update(x.strip() for x in str(v["FixedVersion"]).split(",") if x.strip())
    crit = len(ids_by_sev["CRITICAL"])
    high = len(ids_by_sev["HIGH"] - ids_by_sev["CRITICAL"])
    top = sorted(pkgs.values(), key=lambda p: (SEV_ORDER[p["worst"]], -p["count"], p["pkg"]))[:3]
    return {"critical": crit, "high": high,
            "top": [{"pkg": p["pkg"], "worst": p["worst"], "count": p["count"], "installed": p["installed"],
                     "fixed": max(p["fixed"], key=common.version_key) if p["fixed"] else ""} for p in top]}


def build_report(images: List[dict], errors: List[dict]):
    affected = [i for i in images if i["critical"] or i["high"]]
    affected.sort(key=lambda i: (-i["critical"], -(i["critical"] + i["high"]), i["ref"]))
    tc = sum(i["critical"] for i in affected)
    th = sum(i["high"] for i in affected)
    head = common.join_limited([f"{sorted(i['services'])[0]} {i['critical'] + i['high']}" for i in affected], 4)
    if affected:
        summary = (f"{len(affected)}/{len(images)} running images have fixable HIGH/CRITICAL CVEs "
                   f"({tc}C/{th}H): {head}")
    else:
        summary = f"no fixable HIGH/CRITICAL CVEs in {len(images)} running images"
    if errors:
        summary += f"; {len(errors)} scan errors"
    lines = []
    for i in affected:
        hints = ", ".join(f"{t['pkg']}→{t['fixed']}" for t in i["top"])
        svc = common.join_limited(sorted(i["services"]), 2, "/")
        lines.append(f"{svc} ({display_ref(i['ref'])}): {i['critical']}C/{i['high']}H — {hints}")
    for e in errors:
        lines.append(f"scan failed: {display_ref(e['ref'])} ({e['error']})")
    return summary, lines


def _cvss(v: dict):
    """(vector, score) preferring NVD, then GHSA, then any other source."""
    cv = v.get("CVSS") or {}
    for src in ["nvd", "ghsa"] + sorted(k for k in cv if k not in ("nvd", "ghsa")):
        c = cv.get(src) or {}
        if c.get("V3Vector") or c.get("V40Vector"):
            return c.get("V3Vector") or c.get("V40Vector"), c.get("V3Score") or c.get("V40Score")
    return None, None


def export_rows(result: dict) -> List[dict]:
    """Trivy JSON -> one compact row per (id, package, installed version)."""
    rows: Dict[tuple, dict] = {}
    for r in result.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            key = (v.get("VulnerabilityID"), v.get("PkgName"), v.get("InstalledVersion"))
            if key in rows:
                continue
            vector, score = _cvss(v)
            rows[key] = {
                "id": v.get("VulnerabilityID"), "aliases": v.get("VendorIDs") or [],
                "pkg": v.get("PkgName"), "installed": v.get("InstalledVersion") or "",
                "fixed": v.get("FixedVersion") or "",
                "status": v.get("Status") or ("fixed" if v.get("FixedVersion") else "affected"),
                "severity": v.get("Severity") or "UNKNOWN", "vector": vector, "score": score,
                "cwes": v.get("CweIDs") or [], "title": common.clip(v.get("Title") or "", 120),
                "published": (v.get("PublishedDate") or "")[:10],
                "class": r.get("Class"), "type": r.get("Type"),
            }
    return list(rows.values())


def export_dir() -> str:
    """The export dir and its parent, created root-only (0700)."""
    if SCAN_EXPORT_DIR:
        parent = os.path.dirname(SCAN_EXPORT_DIR.rstrip("/"))
        for d in (parent, SCAN_EXPORT_DIR):
            os.makedirs(d, mode=0o700, exist_ok=True)
    return SCAN_EXPORT_DIR


def export_name(img_id: str) -> str:
    return img_id.replace("sha256:", "")[:64] + ".json"


def write_export(base: dict, result: dict) -> None:
    d = export_dir()
    if not d:
        return
    doc = {**base, "generated": common.utc_now(), "trivy_image": TRIVY_IMAGE, "vulns": export_rows(result)}
    path = os.path.join(d, export_name(base["image_id"]))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, separators=(",", ":"), sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def prune_exports(keep_ids) -> int:
    """Remove exports of images that are no longer running."""
    d = SCAN_EXPORT_DIR
    if not d or not os.path.isdir(d):
        return 0
    keep = {export_name(i) for i in keep_ids}
    n = 0
    for f in os.listdir(d):
        if f.endswith(".json") and f not in keep:
            os.unlink(os.path.join(d, f))
            n += 1
    return n


def scan_one(img_id: str, meta: dict) -> dict:
    # All severities, fixed or not: the export needs them, and summarise_scan
    # filters to fixable HIGH/CRITICAL for the report itself.
    r = trivy(["image", "--image-src", "docker", "--skip-db-update", "--skip-java-db-update",
               "--scanners", "vuln", "--format", "json", "--quiet", "--timeout", "30m", img_id], timeout=2400)
    base = {"image_id": img_id, "ref": meta["ref"], "repo": repo_of(meta["ref"]),
            "services": sorted(meta["services"]), "containers": sorted(meta["containers"])}
    if r.returncode != 0:
        return {**base, "error": common.clip(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"rc={r.returncode}", 80)}
    try:
        result = json.loads(r.stdout)
    except ValueError:
        return {**base, "error": "unparseable trivy output"}
    try:
        write_export(base, result)
    except OSError as e:
        common.log(f"export failed for {display_ref(meta['ref'])}: {e}")
    return {**base, **summarise_scan(result)}


def main() -> int:
    argparse.ArgumentParser().parse_known_args()
    common.read_secrets()
    os.makedirs(TRIVY_CACHE, exist_ok=True)
    pull = common.run(["docker", "pull", "-q", TRIVY_IMAGE], timeout=600)
    if pull.returncode != 0:
        common.log(f"trivy image pull failed: {pull.stderr.strip()[:200]}")
    for what in ("--download-db-only", "--download-java-db-only"):
        r = trivy(["image", what, "--quiet"], timeout=1200)
        if r.returncode != 0:
            common.log(f"trivy {what} failed (rc={r.returncode}); using cached DB: {r.stderr.strip()[:200]}")
    imgs = running_images()
    common.log(f"scanning {len(imgs)} unique running images")
    results = []
    with cf.ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        futs = {ex.submit(scan_one, k, v): k for k, v in imgs.items()}
        for f in cf.as_completed(futs):
            res = f.result()
            results.append(res)
            common.log(f"  {display_ref(res['ref'])}: " + (f"error" if "error" in res else
                                                           f"{res['critical']}C/{res['high']}H"))
    ok = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]
    pruned = prune_exports(imgs.keys())
    if pruned:
        common.log(f"removed {pruned} exports of images no longer running")
    for r in ok:
        r["services"] = set(r["services"])
    summary, lines = build_report(ok, errors)
    for r in ok:
        r["services"] = sorted(r["services"])
    data = {"images": sorted(results, key=lambda r: r["ref"]), "trivy_image": TRIVY_IMAGE}
    common.write_report("cves", "Running-image CVEs (fixable HIGH/CRITICAL)", summary, lines, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
