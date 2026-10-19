#!/usr/bin/env python3
"""Read-only OS patch facts, printed as JSON (runs on each target host via ansible `script`).

Debian/Ubuntu/Raspberry Pi OS: refreshes the package lists (apt-get update),
then lists upgradable packages, which of them come from a security suite,
/var/run/reboot-required, and the running vs newest installed kernel.
macOS: `softwareupdate -l` and `brew outdated` (as the user owning Homebrew).
Nothing is upgraded, installed or restarted.
"""

import json
import os
import platform
import pwd
import re
import subprocess

NUM = re.compile(r"(\d+)")


def run(cmd, timeout=300, env=None, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", str(e)


def vkey(v):
    return [(0, int(p)) if p.isdigit() else (1, p) for p in NUM.split(v) if p]


def os_release():
    d = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                d[k] = v.strip('"')
    except OSError:
        pass
    return d


def debian():
    rel = os_release()
    rc, _, err = run(["apt-get", "update", "-qq"], timeout=600)
    upd_err = err.strip().splitlines()[-1][:160] if rc != 0 and err.strip() else ""
    _, out, _ = run(["apt", "list", "--upgradable"], timeout=120)
    pkgs = []
    for line in out.splitlines():
        m = re.match(r"^([^/\s]+)/(\S+)\s+(\S+)\s+\S+(?:\s+\[upgradable from: ([^\]]+)\])?", line)
        if m:
            suites = m.group(2).split(",")
            pkgs.append({"name": m.group(1), "new": m.group(3), "old": m.group(4) or "",
                         "security": any("security" in s for s in suites)})
    reboot = os.path.exists("/var/run/reboot-required")
    reboot_pkgs = []
    try:
        with open("/var/run/reboot-required.pkgs") as f:
            reboot_pkgs = sorted({l.strip() for l in f if l.strip()})
    except OSError:
        pass
    running = platform.release()
    mods = [m for m in os.listdir("/lib/modules")] if os.path.isdir("/lib/modules") else []
    # Raspberry Pi ships several kernel flavours side by side; compare like with like.
    suffix = re.sub(r"^[\d.\-]+", "", running)
    same = [m for m in mods if re.sub(r"^[\d.\-]+", "", m) == suffix] or mods
    newest = max(same, key=vkey) if same else running
    return {"os": rel.get("PRETTY_NAME", ""), "id": rel.get("ID", ""), "codename": rel.get("VERSION_CODENAME", ""),
            "version_id": rel.get("VERSION_ID", ""), "apt_update_error": upd_err,
            "upgradable": pkgs, "reboot_required": reboot, "reboot_pkgs": reboot_pkgs,
            "kernel_running": running, "kernel_newest": newest}


def parse_softwareupdate(text):
    """Labels of available macOS updates, and whether any of them needs a restart."""
    labels, restart, last = [], False, None
    for line in text.splitlines():
        m = re.match(r"^\s*\*\s*Label:\s*(.+)$", line)
        if m:
            last = m.group(1).strip()
            labels.append(last)
        elif last and re.search(r"Action:\s*restart", line, re.I):
            restart = True
    return labels, restart


def brew_owner():
    for prefix in ("/opt/homebrew", "/usr/local/Homebrew"):
        if os.path.isdir(prefix):
            bin_ = os.path.join("/opt/homebrew" if prefix == "/opt/homebrew" else "/usr/local", "bin", "brew")
            return prefix, bin_, pwd.getpwuid(os.stat(prefix).st_uid).pw_name
    return None, None, None


def macos():
    _, ver, _ = run(["sw_vers", "-productVersion"], timeout=30)
    _, sul, sul_err = run(["softwareupdate", "-l"], timeout=240)
    updates, restart = parse_softwareupdate(sul + "\n" + sul_err)
    prefix, brew, owner = brew_owner()
    outdated, brew_err = [], ""
    if brew and owner and owner != "root":
        home = pwd.getpwnam(owner).pw_dir
        env = {"PATH": f"{os.path.dirname(brew)}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home, "PWD": home,
               "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ANALYTICS": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
        sudo = ["sudo", "-n", "-u", owner, "-H", "env"] + [f"{k}={v}" for k, v in env.items()]
        run(sudo + [brew, "update", "--quiet"], timeout=600, cwd=home)  # refreshes formula metadata only
        rc, out, err = run(sudo + [brew, "outdated", "--json=v2"], timeout=300, cwd=home)
        try:
            j = json.loads(out)
            for f in j.get("formulae", []) + j.get("casks", []):
                outdated.append({"name": f["name"], "old": ",".join(f.get("installed_versions") or [])
                                 if isinstance(f.get("installed_versions"), list) else str(f.get("installed_versions")),
                                 "new": f.get("current_version", "")})
        except ValueError:
            brew_err = (err.strip().splitlines() or [f"rc={rc}"])[-1][:160]
    else:
        brew_err = "Homebrew not found" if not brew else "Homebrew owned by root"
    return {"os": f"macOS {ver.strip()}", "id": "macos", "codename": "", "version_id": ver.strip(),
            "softwareupdate": updates, "softwareupdate_restart": restart,
            "brew_outdated": outdated, "brew_error": brew_err, "brew_owner": owner}


def main():
    res = macos() if platform.system() == "Darwin" else debian()
    res["uptime_days"] = None
    try:
        with open("/proc/uptime") as f:
            res["uptime_days"] = round(float(f.read().split()[0]) / 86400, 1)
    except OSError:
        pass
    print(json.dumps(res))


if __name__ == "__main__":
    main()
