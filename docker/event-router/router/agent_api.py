"""Agent actions: POST /agent/<action> from an OpenClaw agent.

The caller presents its own agent token (Authorization: Bearer), which is
derived from its JIT broker key (docs/design/openclaw-identities.md): the
router and the executor hold only SHA-256 hashes of these tokens, written at
deploy time (/config/agent-tokens.json here, executor.json on the host). The
requester is whoever owns the token; no field can name one. The source
address must also be an allowed client.

The router checks the shape, passes the request to the executor with the
token (the executor checks the token, the allowlists, the tier and every
guard again, and is the only one that acts), logs the decision with the
requester, action, target and result, and returns the executor's reply.
"""

import hashlib
import hmac
import json
import os
import re

ACTIONS = ("collect", "restart_container", "reset_failed")
BODY_KEYS = {"target", "reason", "incident", "approval"}
TARGET = re.compile(r"^(host|container:[a-z0-9][a-z0-9_.-]{0,62}|unit:[A-Za-z0-9][A-Za-z0-9@_.:-]{0,95}"
                    r"|[a-z0-9][a-z0-9_.-]{0,62}|[A-Za-z0-9][A-Za-z0-9@_.:-]{0,95}\.[a-z]+)$")
INCIDENT = re.compile(r"^inc-[0-9a-f]{12}$")
APPROVAL = re.compile(r"^apr-[0-9a-f]{12}$")

# executor reply -> HTTP status
STATUS = {"auth": 401, "tier": 403, "not_allowed": 403, "approval": 403, "rate_limit": 429,
          "cooldown": 429, "unavailable": 503}


class BadRequest(ValueError):
    pass


class Tokens:
    """{requester: sha256 hex of its agent token}, re-read when the file changes."""

    def __init__(self, path):
        self.path = path
        self.mtime = None
        self.hashes = {}

    def load(self):
        try:
            m = os.stat(self.path).st_mtime
        except OSError:
            self.hashes, self.mtime = {}, None
            return self.hashes
        if m != self.mtime:
            try:
                with open(self.path) as f:
                    raw = json.load(f)
                self.hashes = {str(k): str(v) for k, v in (raw or {}).items()
                               if isinstance(v, str) and re.match(r"^[0-9a-f]{64}$", v)}
            except (OSError, ValueError, AttributeError):
                self.hashes = {}
            self.mtime = m
        return self.hashes

    def requester(self, header):
        """The requester for an Authorization header, or None."""
        if not header.startswith("Bearer "):
            return None
        token = header[7:].strip()
        if not 32 <= len(token) <= 256:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        found = None
        for name, want in sorted(self.load().items()):
            if hmac.compare_digest(digest, want):
                found = name
        return found


def parse(action, body):
    if action not in ACTIONS:
        raise BadRequest(f"unknown action; one of {ACTIONS}")
    if not isinstance(body, dict) or set(body) - BODY_KEYS:
        raise BadRequest(f"body takes only {sorted(BODY_KEYS)} (the requester comes from the token)")
    for k in BODY_KEYS:
        if body.get(k) is not None and not isinstance(body[k], str):
            raise BadRequest(f"{k} must be a string")
    target = body.get("target") or ""
    if not TARGET.match(target):
        raise BadRequest("target must be host, container:<name>, unit:<unit> (collect), "
                         "a container name (restart_container) or a unit name (reset_failed)")
    if body.get("incident") and not INCIDENT.match(body["incident"]):
        raise BadRequest("incident must be an incident id (inc-<12 hex>)")
    if body.get("approval") and not APPROVAL.match(body["approval"]):
        raise BadRequest("approval must be the id the first reply gave (apr-<12 hex>)")
    if len(body.get("reason") or "") > 300:
        raise BadRequest("reason: at most 300 characters")
    return {"action": action, "target": target, "reason": body.get("reason") or "",
            "incident": body.get("incident") or None, "approval": body.get("approval") or None}


def http_status(reply):
    if reply.get("ok"):
        return 200
    if reply.get("pending"):
        return 202
    if reply.get("refused"):
        return STATUS.get(reply["refused"], 409)
    return 502


def handle(engine, tokens, header, action, body):
    """(status, reply) for one request; logs the decision either way."""
    requester = tokens.requester(header)
    rec = {"decision": "agent_action", "action": action, "requester": requester}
    if requester is None:
        engine._log(dict(rec, result="unauthorized"))
        return 401, {"error": "unauthorized: present your own agent token"}
    try:
        req = parse(action, body)
    except BadRequest as e:
        engine._log(dict(rec, result="bad_request", detail=str(e)[:200]))
        return 400, {"error": str(e)}
    rec.update(target=req["target"], incident=req["incident"], approval=req["approval"],
               reason=req["reason"][:200])
    if engine.executor is None:
        engine._log(dict(rec, result="unavailable", detail="no executor socket"))
        return 503, {"ok": False, "refused": "unavailable", "detail": "executor not available"}
    reply = engine.executor.agent(dict(req, token=header[7:].strip()))
    result = ("ok" if reply.get("ok") else "pending" if reply.get("pending")
              else reply.get("refused") or "error")
    engine._log(dict(rec, result=result, detail=str(reply.get("detail") or reply.get("error") or "")[:300],
                     **({"approval": reply["approval"]} if reply.get("approval") else {})))
    return http_status(reply), reply
