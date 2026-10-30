#!/usr/bin/env python3
"""Inventory Dolby Vision Profile 7 files in the Plex library.

1. Reads every movie and episode from the Plex API and keeps the media parts
   whose video stream reports DOVIProfile 7.
2. Probes each candidate with dv-probe.sh in the dovi-convert image:
   mediainfo cross-check, FEL/MEL from sampled RPUs, audio/subtitle tracks.
3. Writes the inventory (root-only: it names files) and prints totals only.

Usage: dv-p7-scan.py [--out /root/dv-p7/inventory.json] [--image IMAGE]
                     [--skip-probe]
The Plex token is read from Preferences.xml and never printed.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

PREFS = "/mpool/plex/config/Library/Application Support/Plex Media Server/Preferences.xml"
MEDIA_HOST = "/mpool/plex"  # mounted at /data inside the Plex container
SECTIONS = None  # every movie and show section


def plex_url():
    out = subprocess.run(
        ["docker", "inspect", "plex", "--format",
         "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"],
        capture_output=True, text=True, check=True).stdout.split()
    return f"http://{out[-1]}:32400"


def token():
    with open(PREFS) as f:
        m = re.search(r'PlexOnlineToken="([^"]+)"', f.read())
    if not m:
        sys.exit("no Plex token in Preferences.xml")
    return m.group(1)


class Plex:
    def __init__(self):
        self.base, self.tok = plex_url(), token()

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers={
            "Accept": "application/json", "X-Plex-Token": self.tok})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)["MediaContainer"]


def plex_parts(plex):
    keys = []
    for d in plex.get("/library/sections").get("Directory", []):
        if d["type"] == "movie":
            typ = 1
        elif d["type"] == "show":
            typ = 4
        else:
            continue
        mc = plex.get(f"/library/sections/{d['key']}/all?type={typ}")
        keys += [m["ratingKey"] for m in mc.get("Metadata", [])]
    rows = []
    for i in range(0, len(keys), 40):
        mc = plex.get("/library/metadata/" + ",".join(keys[i:i + 40]))
        for m in mc.get("Metadata", []):
            title = m.get("title", "")
            if m.get("grandparentTitle"):
                title = f"{m['grandparentTitle']} S{m.get('parentIndex', 0):02}E{m.get('index', 0):02} {title}"
            for med in m.get("Media", []):
                for part in med.get("Part", []):
                    v = next((s for s in part.get("Stream", []) if s.get("streamType") == 1), {})
                    rows.append({
                        "ratingKey": m["ratingKey"],
                        "sectionID": m.get("librarySectionID"),
                        "type": m["type"],
                        "title": title,
                        "year": m.get("year"),
                        "file": part.get("file"),
                        "size": part.get("size"),
                        "plex": {k: v.get(k) for k in (
                            "DOVIPresent", "DOVIProfile", "DOVILevel", "DOVIBLPresent",
                            "DOVIELPresent", "DOVIRPUPresent", "DOVIBLCompatID")},
                    })
    return rows


def probe(image, host_path):
    rel = os.path.relpath(host_path, MEDIA_HOST)
    r = subprocess.run(
        ["nice", "-n", "19", "ionice", "-c", "3", "docker", "run", "--rm",
         "-v", f"{MEDIA_HOST}:/data:ro", "--entrypoint", "dv-probe.sh",
         image, f"/data/{rel}"],
        capture_output=True, text=True)
    if r.returncode:
        return {"error": r.stderr[-500:]}
    return json.loads(r.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/dv-p7/inventory.json")
    ap.add_argument("--image", default="gitlab-registry.lab.nkontur.com:443/root/homelab/dovi-convert:latest")
    ap.add_argument("--skip-probe", action="store_true")
    a = ap.parse_args()

    os.umask(0o077)
    rows = plex_parts(Plex())
    p7 = [r for r in rows if r["plex"]["DOVIProfile"] == 7]
    print(f"plex: {len(rows)} media parts, {len(p7)} with DOVIProfile 7", file=sys.stderr)

    old = {}
    if os.path.exists(a.out):  # reuse probes of unchanged files
        for r in json.load(open(a.out)).get("files", []):
            old[(r["file"], r["size"])] = r.get("probe")

    for n, r in enumerate(p7, 1):
        r["host_path"] = os.path.join(MEDIA_HOST, os.path.relpath(r["file"], "/data"))
        if a.skip_probe:
            continue
        r["probe"] = old.get((r["file"], r["size"])) or probe(a.image, r["host_path"])
        if n % 50 == 0:
            print(f"probed {n}/{len(p7)}", file=sys.stderr)

    def tb(xs):
        return round(sum(x["size"] or 0 for x in xs) / 1e12, 2)
    summary = {"p7_files": len(p7), "p7_tb": tb(p7)}
    for el in ("FEL", "MEL", None):
        xs = [r for r in p7 if (r.get("probe") or {}).get("el_type") == el]
        summary[f"{el or 'unknown'}_files"] = len(xs)
        summary[f"{el or 'unknown'}_tb"] = tb(xs)
    summary["probe_errors"] = sum(1 for r in p7 if "error" in (r.get("probe") or {}))
    summary["mediainfo_not_dvhe07"] = sum(
        1 for r in p7 if r.get("probe") and not any(
            "dvhe.07" in (t.get("HDR_Format_Profile") or "") for t in r["probe"].get("mediainfo_hdr", [])))
    st = os.statvfs(MEDIA_HOST)
    summary["free_tb"] = round(st.f_bavail * st.f_frsize / 1e12, 2)

    with open(a.out, "w") as f:
        json.dump({"summary": summary, "files": p7}, f, indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
