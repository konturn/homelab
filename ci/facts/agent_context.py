"""Render OpenClaw's standing context, docker/openclaw/workspace/infra/TOOLS.md.

The hand-written parts (what the agent may do, safety rules, things no file
records) live in docker/openclaw/workspace/infra/TOOLS.md.in and are copied
verbatim. Each line of the form

    <!-- generated: NAME -->

is replaced by the section NAME, rendered from the repo:

    hosts      ansible/inventory.yml hosts and their compose files
    vlans      macvlan networks in docker/docker-compose.yml
    services   compose services per host (profiles, disabled zones marked)
    hardware   inventory/hardware.yml (disks, audio, amps, network devices)
    deps       inventory/dependencies.yml (needs, rules)
    automerge  ci/automerge-policy.yml (limits, denylist, update restrictions)
    botconfig  ci/automerge-policy.yml bot_config, through botconfig.config(),
               the same function the sweep uses, so the two cannot disagree
    docs       docs/*.md

Called by render.py; facts:test fails when the committed TOOLS.md is stale.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import facts

TEMPLATE = "docker/openclaw/workspace/infra/TOOLS.md.in"
OUTPUT = "docker/openclaw/workspace/infra/TOOLS.md"
POLICY = "ci/automerge-policy.yml"
MARK = re.compile(r"^<!-- generated: (\w+) -->$")


def short(host: str) -> str:
    return host.split(".")[0]


def hosts(root: Path) -> dict:
    """{fqdn: merged vars} for every inventory host (group vars, then host)."""
    inv = facts.load_yaml(facts.INVENTORY, root)
    out = {}

    def walk(group, inherited):
        group = group or {}
        merged = {**inherited, **(group.get("vars") or {})}
        for h, hv in (group.get("hosts") or {}).items():
            out[h] = {**merged, **(hv or {})}
        for child in (group.get("children") or {}).values():
            walk(child, merged)

    walk(inv.get("all"), {})
    return out


def compose(root: Path, name: str) -> dict:
    return facts.compose_services(root, f"docker/{name}")


def sec_hosts(root: Path) -> list[str]:
    lines = []
    for h, v in hosts(root).items():
        files = ", ".join(f.strip() for f in v.get("docker_compose_file_names") or []) or "no compose"
        lines.append(f"- **{short(h)}** `{h}` {v.get('ansible_host')}: {files}")
    return lines


def sec_vlans(root: Path) -> list[str]:
    nets = facts.compose_file(root).get("networks") or {}
    items = []
    for name, n in nets.items():
        parent = ((n or {}).get("driver_opts") or {}).get("parent", "")
        m = re.match(r"^\w+\.(\d+)$", parent)
        if (n or {}).get("driver") == "macvlan" and m:
            subnet = (((n.get("ipam") or {}).get("config") or [{}])[0]).get("subnet")
            items.append((int(m.group(1)), f"{m.group(1)} `{name}` {subnet}"))
    return ["- VLANs (router macvlan networks): " + ", ".join(t for _, t in sorted(items)) + "."]


def sec_services(root: Path) -> list[str]:
    lines = []
    for h, v in hosts(root).items():
        names = [f.strip() for f in v.get("docker_compose_file_names") or []]
        if not names:
            continue
        disabled = set(v.get("snapclient_disabled_zones") or [])
        run, oneshot, profile, nocard = [], [], [], []
        for f in names:
            for svc, spec in compose(root, f).items():
                if spec.get("profiles"):
                    profile.append(svc)
                elif svc.startswith("snapclient_") and svc[len("snapclient_"):] in disabled:
                    nocard.append(svc[len("snapclient_"):])
                elif spec.get("restart", "no") == "no":
                    oneshot.append(svc)
                else:
                    run.append(svc)
        line = f"- **{short(h)}**: " + ", ".join(sorted(dict.fromkeys(run)))
        if oneshot:
            line += f"; one-shot: {', '.join(sorted(oneshot))}"
        lines.append(line + ".")
        off = []
        if nocard:
            off.append("snapclient_{" + ",".join(sorted(nocard)) + "} (no sound card, snapclient_disabled_zones)")
        if profile:
            off.append(", ".join(sorted(profile)) + " (compose profile)")
        if off:
            lines.append(f"  Defined, not running: {'; '.join(off)}.")
    return lines


def one_line(s) -> str:
    return " ".join(str(s).split())


def sec_hardware(root: Path) -> list[str]:
    hw = facts.load_yaml(facts.HARDWARE, root)
    lines = []
    pools = {}
    for d in hw["disks"]:
        if d.get("zpool"):
            pools.setdefault(d["zpool"]["pool"], {}).setdefault(d["zpool"]["vdev"], 0)
            pools[d["zpool"]["pool"]][d["zpool"]["vdev"]] += 1
    for pool, vdevs in pools.items():
        lines.append(f"- `{pool}`: " + ", ".join(f"{v} ({n} disks)" for v, n in sorted(vdevs.items()))
                     + ", every disk LUKS (crypttab generated from the data).")
    for d in hw["disks"]:
        if d["role"] in ("boot", "persistent_data"):
            lines.append(f"- {d['role']}: {d['model']} in {d['slot']}, holds {', '.join(d.get('holds') or [])}.")
    for d in hw["disks"]:
        if d.get("known_issue"):
            lines.append(f"- {d['slot']} ({d['luks']['mapper']}): {one_line(d['known_issue'])}")
        elif d["role"] == "spare":
            lines.append(f"- {d['slot']} ({d['luks']['mapper']}, {d['model']}): hot spare in `{d['zpool']['pool']}`.")
        elif d["role"] == "retired":
            lines.append(f"- {d['slot']} ({d['luks']['mapper']}): retired, out of the pool.")
    zones = hw.get("audio_zones") or []
    lines.append("- Audio: speaker zone -> Monoprice amp input -> Snapcast stream -> USB card (udev rule "
                 "generated, by port). Streams: " + ", ".join(z["id"] for z in zones) + ".")
    for name, amp in (hw.get("amps") or {}).items():
        if amp.get("connected", True) is False:
            lines.append(f"- Amp {name}: {one_line(amp.get('note') or 'not connected.')}")
            continue
        ins = ", ".join(f"{n} {i['stream']}" for n, i in sorted((amp.get("inputs") or {}).items()) if i.get("stream"))
        zs = ", ".join(f"{z['name'].removesuffix(' Speakers')} ({z['entity'].removeprefix('media_player.')})"
                       for _, z in sorted((amp.get("zones") or {}).items()))
        lines.append(f"- Amp {name} (HA {amp['ha_port']}), inputs {ins}; zones {zs}."
                     + (f" {one_line(amp['note'])}" if amp.get("note") else ""))
    for name, dev in (hw.get("network_devices") or {}).items():
        state = " ".join(one_line(dev[k]) for k in ("note", "known_issue") if dev.get(k))
        lines.append(f"- {dev['desc']}: {state}")
    return lines


def sec_deps(root: Path) -> list[str]:
    deps = facts.load_yaml(facts.DEPENDENCIES, root)
    nodes = deps.get("nodes") or {}
    lines = []
    for name, n in nodes.items():
        needs = [x for x in n.get("needs") or [] if x != "persistent_data"]
        if needs:
            where = f" ({n['host']})" if n.get("host") else ""
            lines.append(f"- {name}{where} needs {', '.join(needs)}.")
    lines.append("- Every router container needs `/persistent_data` (the 870 EVO, Docker's data-root).")
    for name, n in nodes.items():
        for rule in n.get("rules") or []:
            lines.append(f"- **Rule ({name}):** {one_line(rule)}")
    return lines


def load_policy(root: Path) -> dict:
    return facts.load_yaml(POLICY, root)


def sec_automerge(root: Path) -> list[str]:
    pol = load_policy(root)
    ft = pol.get("fast_track") or {}
    lines = [f"- Limits: {pol['max_per_24h']} general merges per rolling 24h"
             + (f", plus {ft['max_per_24h']} fast-track merges (internet-facing images)" if ft.get("enabled") else "")
             + "; one merge per sweep run."]
    deny = sorted((pol.get("denylist") or {}).get("services") or {})
    lines.append(f"- Never auto-merged (denylist): {', '.join(deny)}; majors; anything but a compose image line.")
    restricted = {}
    for svc, spec in (pol.get("services") or {}).items():
        if (spec or {}).get("updates"):
            restricted.setdefault("/".join(spec["updates"]), []).append(svc)
    for kinds, svcs in sorted(restricted.items()):
        lines.append(f"- {kinds} updates only: {', '.join(sorted(svcs))}.")
    backed = sorted(s for s, spec in (pol.get("services") or {}).items() if (spec or {}).get("pre_upgrade_backup"))
    if backed:
        lines.append(f"- Version updates of {', '.join(backed)} are backed up first and restored on rollback.")
    zm = sorted(s for s, spec in (pol.get("services") or {}).items() if (spec or {}).get("zero_minor_is_minor"))
    if zm:
        lines.append(f"- 0.x minors count as minors: {', '.join(zm)}.")
    return lines


def max_bot_files(root: Path) -> int:
    m = re.search(r"is_bot_config_author\([^)]*\) and len\(files\) <= (\d+)",
                  (root / "ci/automerge/automerge.py").read_text(encoding="utf-8"))
    if not m:
        raise facts.FactsError("agent_context: cannot find the bot-config file limit in ci/automerge/automerge.py")
    return int(m.group(1))


def sec_botconfig(root: Path) -> list[str]:
    sys.path.insert(0, str(root / "ci" / "automerge"))
    import botconfig  # noqa: E402  (pure functions; the sweep uses the same config())
    pol = load_policy(root)
    cfg = botconfig.config(pol)
    if not cfg["enabled"]:
        return ["- The bot-config class is disabled: nothing of yours is merged without Noah."]

    def ticks(xs):
        return ", ".join(f"`{x}`" for x in xs)

    deny = sorted(s for s in set((pol.get("denylist") or {}).get("services") or {}) | set(cfg["denied_services"])
                  if not s.endswith("-init"))
    lo, hi = cfg["min_ratio"], cfg["max_ratio"]
    return [
        f"- Author {ticks(cfg['authors'])}, at most {cfg['max_per_24h']} bot-config merges per 24h "
        f"(they also count against the general budget); at most {max_bot_files(root)} files; "
        "no added, deleted, renamed or mode-changed file; no script (by extension or `#!`).",
        f"- Never (wins over everything): {ticks(cfg['never'])}.",
        f"- **Compose** ({ticks(cfg['paths']['compose'])}): the value of an existing "
        f"`deploy.resources` memory/cpus limit or reservation, within {lo:g}x-{hi:g}x of the old value; an "
        "existing `*LOG_LEVEL`/`*LOGLEVEL` env value, to a known level; comment lines without `{ }` "
        f"outside block scalars. Not for {', '.join(deny)}, init containers or any openclaw/moltbot service.",
        f"- **Alert rules** ({ticks(cfg['paths']['alerts'])}), existing rules only: `for`, summary/description, "
        "severity (critical/warning/info), threshold numbers. No rule added, removed or reordered.",
        f"- **Dashboards** ({ticks(cfg['paths']['dashboard'])}): keep the uid, add no datasource.",
        f"- **Docs** ({ticks(cfg['paths']['docs'])}), except "
        + ", ".join(x.removeprefix("docs/") for x in cfg["docs_exclude"]) + " under docs/.",
    ]


def sec_docs(root: Path) -> list[str]:
    names = sorted((p.stem for p in (root / "docs").glob("*.md")), key=str.lower)
    return ["- `docs/`: " + ", ".join(names) + "."]


SECTIONS = {"hosts": sec_hosts, "vlans": sec_vlans, "services": sec_services, "hardware": sec_hardware,
            "deps": sec_deps, "automerge": sec_automerge, "botconfig": sec_botconfig, "docs": sec_docs}

# Every file a section reads (render.py's CI job watches these).
INPUTS = [TEMPLATE, facts.INVENTORY, facts.HARDWARE, facts.DEPENDENCIES, POLICY, "ci/automerge/botconfig.py",
          "ci/automerge/automerge.py", "docker/docker-compose*.yml", "docs/*.md"]


def render(root: Path = facts.ROOT) -> str:
    out = ["<!-- GENERATED by ci/facts/render.py from TOOLS.md.in and the repo data. Edit those, not this file. -->"]
    for line in (root / TEMPLATE).read_text(encoding="utf-8").splitlines():
        m = MARK.match(line.strip())
        if not m:
            out.append(line)
            continue
        name = m.group(1)
        if name not in SECTIONS:
            raise facts.FactsError(f"{TEMPLATE}: unknown section {name} (known: {', '.join(SECTIONS)})")
        out.extend(SECTIONS[name](root))
    return "\n".join(out).rstrip("\n") + "\n"
