"""What facts:drift does with a difference, instead of opening an issue per run.

Three kinds (inventory/README.md, "What happens to drift"):

  software  the repo is right and the router is not (a declared container not
            running, a declared systemd unit inactive): fixed in place, the
            way the deploy would (`docker compose up -d --no-deps <service>`
            on the compose file the last deploy rendered, `systemctl start`),
            then checked again. Only a failed fix is reported.
  record    a physical change that explains itself (a disk replaced in a
            bay, a disk moved to another bay, a new disk that is already a
            healthy pool member or spare, a disk gone from the pool and the
            bays): an MR updating inventory/hardware.yml (and the files
            rendered from it) is opened for review, label `facts-record`.
            The bot never merges it: it is a record, reviewed like any other.
  hands     everything else (faulted disk, missing device, card on the wrong
            port, endpoint down after a fix was tried): one living issue,
            labels `facts-drift` + `needs-hands`, rewritten in place (no
            notes, no notifications per run) and closed when reality matches
            the repo again.

Pure functions plus a small GitLab client; drift.py drives them.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

import facts

# Never restarted automatically: a restarted Vault comes up sealed, and GitLab
# runs this very job. A human looks at these.
NO_AUTO_REMEDIATE = {"vault", "gitlab"}
COMPOSE_DIR = "/persistent_data/application/ansible_state"
COMPOSE_PROJECT = "docker"
HEALTHY_LEAF = {"ONLINE", "AVAIL", "INUSE"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
RECORD_BRANCH = "facts/drift-record"
RECORD_LABEL = "facts-record"
REPORT_LABELS = ["facts-drift", "needs-hands"]
REPORT_MARKER = "<!-- facts-drift-living-report -->"
REPORT_TITLE = "Needs hands: the router differs from inventory/ (facts:drift living report)"


# ---------------------------------------------------------------------------
# software: remediation commands
# ---------------------------------------------------------------------------

def remediation_command(action: dict, services: dict) -> tuple[str | None, str]:
    """(shell command run on the router, or None, and why not)."""
    if action.get("type") == "container":
        name = action["name"]
        svc = services.get(name)
        if name in NO_AUTO_REMEDIATE or svc in NO_AUTO_REMEDIATE:
            return None, f"{name} is never restarted automatically"
        if not svc or not SAFE_NAME.match(svc):
            return None, f"no compose service for container {name}"
        return (f"cd {COMPOSE_DIR} && docker compose -p {COMPOSE_PROJECT} -f docker-compose.yml "
                f"up -d --no-deps {svc}"), ""
    if action.get("type") == "systemd":
        unit = action["unit"]
        if not SAFE_NAME.match(unit):
            return None, f"unsafe unit name {unit}"
        return f"systemctl start {unit}", ""
    return None, "no automatic fix for this"


def container_services(root: Path = facts.ROOT) -> dict:
    """{container name: compose service} for the router's compose file."""
    return {(spec.get("container_name") or svc): svc for svc, spec in facts.compose_services(root).items()}


# ---------------------------------------------------------------------------
# record: disks
# ---------------------------------------------------------------------------

def normalize_model(live_model: str) -> str:
    """lsblk "WDC WD241KFGX-68" -> "WD241KFGX"; others unchanged."""
    m = live_model.strip()
    if m.startswith("WDC "):
        m = m[4:].split("-")[0]
    return m


def slot_of_path(hw: dict, path: str) -> str | None:
    for name, c in (hw.get("disk_controllers") or {}).items():
        pre, _, post = c["by_path"].partition("{slot}")
        if path.startswith(pre) and path.endswith(post) and len(path) > len(pre) + len(post):
            return f"{name}/{path[len(pre):len(path) - len(post)]}"
    return None


def _identity(model: str, suffix: str) -> tuple:
    return (re.sub(r"[^a-z0-9]", "", model.lower()), suffix)


def _leaf(live: dict, mapper: str):
    for pname, pool in (live.get("zpools") or {}).items():
        leaf = (pool.get("leaves") or {}).get(mapper)
        if leaf:
            return pname, leaf
    return None, None


def propose_disks(hw: dict, live: dict, today: str):
    """Disk records that explain themselves.

    Returns (disks, changes, covered, hands): the new `disks` list (None when
    nothing to record), one line per change, the slots and mappers whose drift
    the change explains, and drift that needs hands instead (a new disk that
    is in no pool).
    """
    disks_live = live.get("disks")
    if disks_live is None or live.get("zpools") is None:
        return None, [], set(), []
    from drift import model_matches  # noqa: PLC0415 (drift imports this module)

    live_by_slot = {}
    for path, got in disks_live.items():
        slot = slot_of_path(hw, path)
        if slot:
            live_by_slot[slot] = got
    declared = [d for d in hw["disks"]]
    by_slot = {d["slot"]: d for d in declared}

    def same(d, got):
        return (d.get("role") != "empty" and got and str(d.get("serial_suffix")) == got["serial_suffix"]
                and model_matches(d.get("model", ""), got["model"]))

    new_by_slot: dict[str, dict] = {}
    changes, covered, hands = [], set(), []
    placed = set()  # ids of declared entries that found their disk
    for slot, got in sorted(live_by_slot.items()):
        here = by_slot.get(slot)
        if here and same(here, got):
            placed.add(id(here))
            continue
        moved = next((d for d in declared if d is not here and same(d, got)), None)
        if moved:
            entry = copy.deepcopy(moved)
            entry["slot"] = slot
            new_by_slot[slot] = entry
            placed.add(id(moved))
            mapper = (moved.get("luks") or {}).get("mapper")
            changes.append(f"{moved['slot']} -> {slot}: {moved['model']} ...{moved['serial_suffix']} ({mapper}) moved.")
            covered |= {f"slot:{slot}", f"slot:{moved['slot']}", f"mapper:{mapper}"}
            continue
        mapper = got["mappers"][0] if got.get("mappers") else None
        pname, leaf = _leaf(live, mapper) if mapper else (None, None)
        if not leaf or not got.get("luks_uuid"):
            hands.append((f"slot:{slot}", f"{slot}: new disk {got['model']} ...{got['serial_suffix']} "
                                          f"(/dev/{got['dev']}) is in no pool; decide what it is for."))
            continue
        entry = {"slot": slot, "model": normalize_model(got["model"]), "serial_suffix": got["serial_suffix"],
                 "role": "spare" if leaf["vdev"] == "spares" else "data",
                 "luks": {"mapper": mapper, "uuid": got["luks_uuid"]},
                 "zpool": {"pool": pname, "vdev": leaf["vdev"]},
                 "note": f"Recorded by facts:drift {today}."}
        new_by_slot[slot] = entry
        was = f", replacing {here['model']} ...{here['serial_suffix']}" if here and here.get("role") != "empty" else ""
        changes.append(f"{slot}: new {entry['model']} ...{entry['serial_suffix']} ({mapper}, {entry['role']} "
                       f"in {pname}/{leaf['vdev']}){was}.")
        covered |= {f"slot:{slot}", f"mapper:{mapper}"}
    # declared disks that are nowhere now
    removed = set()
    for d in declared:
        if d.get("role") == "empty" or id(d) in placed:
            continue
        mapper = (d.get("luks") or {}).get("mapper")
        _, leaf = _leaf(live, mapper) if mapper else (None, None)
        if leaf:
            continue  # still a pool member: a missing disk, hands (check_disks reports it)
        removed.add(id(d))
        changes.append(f"{d['slot']}: {d.get('model')} ...{d.get('serial_suffix')} ({mapper}) is gone from the "
                       "bays and the pool; entry removed.")
        covered |= {f"slot:{d['slot']}", f"mapper:{mapper}"}
    if not changes:
        return None, [], set(), hands
    moved_src = {id(d) for d in declared for e in new_by_slot.values()
                 if e is not None and d.get("role") != "empty" and e is not d and e.get("slot") != d["slot"]
                 and {**d, "slot": e["slot"]} == e}
    out, done = [], set()
    for d in declared:
        if d["slot"] in new_by_slot:
            done.add(d["slot"])
            out.append(new_by_slot[d["slot"]])
        elif id(d) in moved_src or id(d) in removed:
            if d["slot"] not in live_by_slot:
                out.append({"slot": d["slot"], "role": "empty",
                            "note": f"Vacated; recorded by facts:drift {today}."})
        else:
            out.append(d)
    for slot in sorted((s for s in new_by_slot if s not in done), key=lambda s: _slot_key(hw, s)):
        ctrl = slot.split("/")[0]
        same_ctrl = [i for i, e in enumerate(out) if e["slot"].split("/")[0] == ctrl
                     and _slot_key(hw, e["slot"]) < _slot_key(hw, slot)]
        pos = same_ctrl[-1] + 1 if same_ctrl else len(out)
        out.insert(pos, new_by_slot[slot])
    return out, changes, covered, hands


def _slot_key(hw: dict, slot: str):
    ctrl, _, local = slot.partition("/")
    order = list((hw.get("disk_controllers") or {}).keys())
    m = re.match(r"^(\D*)(\d+)$", local)
    return (order.index(ctrl) if ctrl in order else 99, (m.group(1), int(m.group(2))) if m else (local, -1))


# ---------------------------------------------------------------------------
# record: rewriting inventory/hardware.yml without losing its comments
# ---------------------------------------------------------------------------

ENTRY_START = re.compile(r"^  - \{?slot: (\S+?)[,\s}]")


def _segments(lines: list[str]):
    """Split the disks: block into comment/blank lines and entries (by slot)."""
    segs, cur = [], None
    for line in lines:
        m = ENTRY_START.match(line + " ")
        if m:
            cur = {"slot": m.group(1), "lines": [line]}
            segs.append(cur)
        elif cur is not None and line.startswith("     ") or (cur is not None and line.startswith("    ")):
            cur["lines"].append(line)
        else:
            cur = None
            segs.append({"slot": None, "lines": [line]})
    return segs


def emit_entry(e: dict) -> list[str]:
    simple = set(e) <= {"slot", "model", "serial_suffix", "role", "luks", "zpool"} and e.get("zpool")
    if simple:
        head = f"  - {{slot: {e['slot']}, model: {e['model']}, serial_suffix: {e['serial_suffix']}, role: {e['role']},"
        luks = ", ".join(f"{k}: {v}" for k, v in e["luks"].items())
        zp = ", ".join(f"{k}: {v}" for k, v in e["zpool"].items())
        return [head, f"     luks: {{{luks}}}, zpool: {{{zp}}}}}"]
    text = yaml.safe_dump([e], sort_keys=False, default_flow_style=None, width=74, allow_unicode=True)
    return ["  " + line for line in text.rstrip("\n").splitlines()]


def rewrite_hardware(text: str, old_disks: list, new_disks: list) -> str:
    """Rewrite the disks: block of hardware.yml to new_disks, keeping the text
    (comments, notes, formatting) of every entry that did not change and the
    entry text of a disk that only moved (its slot is edited)."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("disks:"))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i] and not lines[i].startswith(" ")), len(lines))
    segs = _segments(lines[start + 1:end])
    old_by_slot = {d["slot"]: d for d in old_disks}
    seg_by_slot = {s["slot"]: s for s in segs if s["slot"]}

    def text_for(e):
        old = old_by_slot.get(e["slot"])
        if old == e:
            return seg_by_slot[e["slot"]]["lines"]
        src = next((s for s, o in old_by_slot.items() if s != e["slot"] and {**o, "slot": e["slot"]} == e), None)
        if src:
            return [re.sub(rf"slot: {re.escape(src)}(?=[,\s}}])", f"slot: {e['slot']}", l, count=1)
                    for l in seg_by_slot[src]["lines"]]
        return emit_entry(e)

    # entries not in the old file go right after their predecessor in new_disks
    after: dict = {}
    prev = None
    for e in new_disks:
        if e["slot"] not in old_by_slot:
            after.setdefault(prev, []).append(e)
        else:
            prev = e["slot"]
    new_by_slot = {e["slot"]: e for e in new_disks}
    out = []

    def emit_after(slot):
        for e in after.get(slot, []):
            out.extend(text_for(e))
            emit_after(e["slot"])

    emit_after(None)
    for seg in segs:
        if seg["slot"] is None:
            out.extend(seg["lines"])
            continue
        e = new_by_slot.get(seg["slot"])
        if e is not None:
            out.extend(text_for(e))
        emit_after(seg["slot"])
    result = "\n".join(lines[:start + 1] + out + lines[end:])
    if (yaml.safe_load(result) or {}).get("disks") != new_disks:
        raise facts.FactsError("rewrite_hardware: the rewritten file does not load back as the proposed disks")
    return result


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------

class GitLab:
    def __init__(self, token: str, api: str | None = None, project: str | None = None, opener=None):
        self.token = token
        self.api = (api or os.environ.get("CI_API_V4_URL", "https://gitlab.lab.nkontur.com/api/v4")).rstrip("/")
        self.project = project or os.environ.get("CI_PROJECT_ID", "4")
        self.opener = opener or urllib.request.urlopen

    def req(self, method: str, path: str, params=None, body=None, raw=False):
        url = f"{self.api}/{path.lstrip('/')}".replace("{p}", self.project)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("PRIVATE-TOKEN", self.token)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        for attempt in range(3):
            try:
                with self.opener(r, timeout=30) as resp:
                    payload = resp.read()
                    if raw:
                        return payload.decode()
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as e:
                if e.code == 404 and method == "GET":
                    return None
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read()[:300]!r}")
            except (urllib.error.URLError, OSError):
                if attempt == 2 or method != "GET":
                    raise
                time.sleep(1 + attempt)


def record_mr(gl: GitLab, files: dict[str, str], changes: list[str], report_url: str | None) -> str:
    """Open or refresh the record MR. Returns what happened."""
    current = {}
    branch_exists = gl.req("GET", "projects/{p}/repository/branches/" + urllib.parse.quote(RECORD_BRANCH, safe=""))
    if branch_exists:
        for path in files:
            current[path] = gl.req("GET", "projects/{p}/repository/files/%s/raw" % urllib.parse.quote(path, safe=""),
                                   {"ref": RECORD_BRANCH}, raw=True)
    mrs = gl.req("GET", "projects/{p}/merge_requests",
                 {"state": "opened", "source_branch": RECORD_BRANCH, "per_page": 5}) or []
    if branch_exists and mrs and all(current.get(p) == c for p, c in files.items()):
        return f"record MR !{mrs[0]['iid']} already proposes this"
    gl.req("POST", "projects/{p}/repository/commits", body={
        "branch": RECORD_BRANCH, "start_branch": "main", "force": True,
        "commit_message": "facts: record hardware changes found by facts:drift\n\n" + "\n".join(f"- {c}" for c in changes),
        "actions": [{"action": "update", "file_path": p, "content": c} for p, c in files.items()],
    })
    desc = ("facts:drift found physical changes that explain themselves and proposes recording them "
            "(inventory/README.md, \"What happens to drift\"). Check them against what was done "
            "by hand, then merge; the files rendered from the data are updated with it.\n\n"
            + "\n".join(f"- {c}" for c in changes)
            + (f"\n\nDrift run: {report_url}" if report_url else "")
            + "\n\nOpened by the scheduled facts:drift job. Not auto-merged.")
    if mrs:
        gl.req("PUT", "projects/{p}/merge_requests/%s" % mrs[0]["iid"], body={"description": desc})
        return f"record MR !{mrs[0]['iid']} updated"
    users = gl.req("GET", "users", {"username": "root"}) or []
    mr = gl.req("POST", "projects/{p}/merge_requests", body={
        "source_branch": RECORD_BRANCH, "target_branch": "main", "remove_source_branch": True,
        "title": "facts: record hardware changes found by facts:drift", "description": desc,
        "labels": RECORD_LABEL, **({"assignee_id": users[0]["id"]} if users else {}),
    })
    return f"record MR !{(mr or {}).get('iid')} opened"


ITEM_RE = re.compile(r"^- (?P<text>.*) _\(since (?P<since>\d{4}-\d{2}-\d{2})\)_ <!-- k:(?P<key>[^ ]+) -->$")


def living_report(items: list[tuple[str, str, str]], previous: str, today: str, job_url: str | None) -> str:
    """items: (section, key, text). Keeps each item's first-seen date."""
    since = {}
    for line in (previous or "").splitlines():
        m = ITEM_RE.match(line)
        if m:
            since[m.group("key")] = m.group("since")
    out = [REPORT_MARKER,
           "Hardware and services that differ from `inventory/*.yml` and need someone on site or a "
           "decision. facts:drift rewrites this list on every run (no notes, no notifications) and "
           "closes the issue when nothing is left. Software drift is fixed automatically and "
           "self-explaining hardware changes arrive as `facts-record` MRs; neither shows up here. "
           "See inventory/README.md, \"What happens to drift\".", ""]
    section = None
    for sec, key, text in items:
        if sec != section:
            out += ["", f"### {sec}"]
            section = sec
        out.append(f"- {text} _(since {since.get(key, today)})_ <!-- k:{key} -->")
    out += ["", f"Last checked {today}" + (f": {job_url}" if job_url else ".")]
    return "\n".join(out) + "\n"


def _body_without_footer(text: str) -> str:
    return "\n".join(l for l in (text or "").splitlines() if not l.startswith("Last checked "))


def update_issue(gl: GitLab, items, today: str, job_url: str | None) -> str:
    issues = gl.req("GET", "projects/{p}/issues", {"state": "opened", "labels": "facts-drift", "per_page": 5}) or []
    issue = issues[0] if issues else None
    if not items:
        if issue:
            gl.req("POST", "projects/{p}/issues/%s/notes" % issue["iid"],
                   body={"body": "Nothing needs hands any more: the router matches inventory/*.yml. Closing."})
            gl.req("PUT", "projects/{p}/issues/%s" % issue["iid"], body={"state_event": "close"})
            return f"closed #{issue['iid']}"
        return "nothing needs hands"
    body = living_report(items, (issue or {}).get("description") or "", today, job_url)
    if issue:
        if _body_without_footer(issue.get("description")) == _body_without_footer(body) \
                and set(REPORT_LABELS) <= set(issue.get("labels") or []):
            return f"#{issue['iid']} unchanged"
        gl.req("PUT", "projects/{p}/issues/%s" % issue["iid"], body={
            "description": body, "title": REPORT_TITLE,
            "labels": ",".join(sorted(set(issue.get("labels") or []) | set(REPORT_LABELS)))})
        return f"#{issue['iid']} rewritten"
    new = gl.req("POST", "projects/{p}/issues", body={"title": REPORT_TITLE, "labels": ",".join(REPORT_LABELS),
                                                    "description": body})
    return f"opened #{(new or {}).get('iid')}"


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
