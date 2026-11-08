"""Outcome records (doc 8.2 step 1): OpenClaw's structured result for every
escalation it handled, POSTed to /outcome and written to the decision log.

  {"incident": "inc-0123456789ab",          # or null with found_by (8.4: missed incident)
   "root_cause": "coordinator stopped forwarding frames after a firmware update",
   "root_node": "slzb_coordinator",         # a dependencies.yml node, or ""
   "fix": "restart_container:zigbee2mqtt",  # an allowlist entry, or ""
   "fix_kind": "allowlisted_action",
   "playbook_able": true,
   "existing_playbook": null,               # a playbook id, if one exists for it
   "noise": false,
   "mr": null,                              # MR iid in root/homelab, or null
   "found_by": null}                        # e.g. "cron:main-pipeline-fix"

The schema is closed. `fix` must name an allowlist entry: the router rejects
anything else, so a record can never smuggle a command into a playbook MR.
Free text (root_cause) is stored for humans and never parsed.
"""

import re

from .config import OUTCOME_FIX_ACTIONS

FIX_KINDS = ("allowlisted_action", "config_change", "hardware", "external", "none_needed", "unknown")
KEYS = {"incident", "root_cause", "root_node", "fix", "fix_kind", "playbook_able",
        "existing_playbook", "noise", "mr", "found_by"}
_INC = re.compile(r"^inc-[0-9a-f]{12}$")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.@-]{0,62}$")
_JOB = re.compile(r"^[0-9]{1,12}$")
_FOUND_BY = re.compile(r"^(cron|noah|audit|drift):[a-z0-9][a-z0-9_.-]{0,62}$")


class BadOutcome(ValueError):
    pass


def validate(body, graph, playbooks, incident_exists):
    if not isinstance(body, dict):
        raise BadOutcome("outcome must be a JSON object")
    unknown = set(body) - KEYS
    if unknown:
        raise BadOutcome(f"unknown fields {sorted(unknown)}")
    rec = {}
    inc = body.get("incident")
    found_by = body.get("found_by")
    if inc is None:
        if not isinstance(found_by, str) or not _FOUND_BY.match(found_by):
            raise BadOutcome("an outcome without an incident needs found_by (cron:<name>, audit:<name>, ...)")
    elif not isinstance(inc, str) or not _INC.match(inc) or not incident_exists(inc):
        raise BadOutcome("incident must be an existing incident id")
    elif found_by is not None and (not isinstance(found_by, str) or not _FOUND_BY.match(found_by)):
        raise BadOutcome("found_by must look like cron:<name>")
    rec["incident"] = inc
    rec["found_by"] = found_by

    rc = body.get("root_cause")
    if not isinstance(rc, str) or not rc.strip():
        raise BadOutcome("root_cause is required")
    rec["root_cause"] = rc.strip()[:500]

    node = body.get("root_node") or ""
    if not isinstance(node, str) or (node and node not in graph.nodes):
        raise BadOutcome("root_node must be a dependencies.yml node or empty")
    rec["root_node"] = node

    kind = body.get("fix_kind")
    if kind not in FIX_KINDS:
        raise BadOutcome(f"fix_kind must be one of {FIX_KINDS}")
    rec["fix_kind"] = kind

    fix = body.get("fix") or ""
    if not isinstance(fix, str):
        raise BadOutcome("fix must be a string")
    if fix:
        action, _, target = fix.partition(":")
        if action not in OUTCOME_FIX_ACTIONS:
            raise BadOutcome(f"fix must be <action>:<target> with an allowlisted action {OUTCOME_FIX_ACTIONS}")
        if not (_NAME.match(target) or _JOB.match(target)):
            raise BadOutcome("fix target must be a plain name or a job id")
    if kind == "allowlisted_action" and not fix:
        raise BadOutcome("fix_kind allowlisted_action needs a fix")
    if kind != "allowlisted_action" and fix:
        raise BadOutcome("only fix_kind allowlisted_action carries a fix")
    rec["fix"] = fix

    for k in ("playbook_able", "noise"):
        v = body.get(k, False)
        if not isinstance(v, bool):
            raise BadOutcome(f"{k} must be true or false")
        rec[k] = v
    if rec["playbook_able"] and kind != "allowlisted_action":
        raise BadOutcome("playbook_able needs a single allowlisted action as the fix")

    pb = body.get("existing_playbook")
    if pb is not None and pb not in playbooks:
        raise BadOutcome("existing_playbook must be a playbook id or null")
    rec["existing_playbook"] = pb

    mr = body.get("mr")
    if mr is not None and (isinstance(mr, bool) or not isinstance(mr, int) or mr <= 0):
        raise BadOutcome("mr must be an MR iid (integer) or null")
    rec["mr"] = mr
    return rec
