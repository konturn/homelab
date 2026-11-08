"""Event intake: normalise webhook payloads (section 4.3).

Every event becomes {source, kind, fp, rule_uid, alertname, labels, status,
starts_at}. Annotations and other free text are kept only for the log and are
never used for matching or for building an action (section 7.1).
"""

import re
from dataclasses import dataclass, field

_RULE_URL = re.compile(r"/alerting/grafana/([A-Za-z0-9_-]{1,64})/view")
_LABEL_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_FP = re.compile(r"^[0-9a-f]{1,32}$")
MAX_ALERTS = 200
MAX_LABEL_VALUE = 256
ERROR_ALERTNAMES = ("DatasourceError", "DatasourceNoData")


@dataclass
class Event:
    source: str
    kind: str            # alert | datasource_error | ci_orphan
    fp: str              # source-qualified fingerprint
    status: str          # firing | resolved
    rule_uid: str = ""
    alertname: str = ""
    labels: dict = field(default_factory=dict)
    starts_at: str = ""
    summary: str = ""    # log only

    def to_dict(self):
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


class BadPayload(ValueError):
    pass


def _clean_labels(raw):
    if not isinstance(raw, dict):
        raise BadPayload("labels must be an object")
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not (_LABEL_KEY.match(k) or k.startswith("__")):
            continue
        if not isinstance(v, str):
            continue
        out[k] = v[:MAX_LABEL_VALUE]
    return out


def from_grafana(payload):
    """Grafana webhook contact point payload -> list of Events."""
    if not isinstance(payload, dict):
        raise BadPayload("payload must be an object")
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        raise BadPayload("payload has no alerts list")
    events = []
    for a in alerts[:MAX_ALERTS]:
        if not isinstance(a, dict):
            continue
        status = a.get("status")
        if status not in ("firing", "resolved"):
            continue
        labels = _clean_labels(a.get("labels") or {})
        fp = str(a.get("fingerprint") or "")
        if not _FP.match(fp):
            continue
        rule_uid = labels.pop("__alert_rule_uid__", "")
        if not rule_uid:
            m = _RULE_URL.search(str(a.get("generatorURL") or ""))
            rule_uid = m.group(1) if m else labels.get("rule_uid", "")
        labels = {k: v for k, v in labels.items() if not k.startswith("__")}
        alertname = labels.get("alertname", "")
        kind = "datasource_error" if alertname in ERROR_ALERTNAMES else "alert"
        summary = ""
        ann = a.get("annotations")
        if isinstance(ann, dict) and isinstance(ann.get("summary"), str):
            summary = ann["summary"][:300]
        events.append(Event(
            source="grafana", kind=kind, fp=f"grafana:{fp}", status=status,
            rule_uid=rule_uid, alertname=alertname, labels=labels,
            starts_at=str(a.get("startsAt") or "")[:40], summary=summary))
    return events


# ---- GitLab webhooks (phase 3, routing table rows 9 and 10) -------------------
# Runner/system failures: retrying may help (4.5 retry_ci_job). Never a
# script failure (the job's own code failed) or the job's own timeout.
SYSTEM_FAILURES = ("runner_system_failure", "stuck_or_timeout_failure", "api_failure",
                   "scheduler_failure", "data_integrity_failure")
_JOB_NAME = re.compile(r"^[A-Za-z0-9_.:/ -]{1,100}$")
_TASK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# A <host>:deploy-service failure was already rolled back by the auto-merge
# sweep (revert MR, rollback issue, mail): the digest, not OpenClaw (row 9).
_DEPLOY_SERVICE = re.compile(r"^[a-z0-9-]+:deploy-service$")


def _int(v):
    if isinstance(v, bool) or not isinstance(v, (int, str)):
        return None
    try:
        return int(v)
    except ValueError:
        return None


def from_gitlab(payload, projects, branch="main"):
    """GitLab pipeline/job webhook payload -> list of Events (at most one).

    Pipelines on `branch`: failed -> `pipeline_failed` (firing), success ->
    the same fingerprint resolved. The fingerprint is per project, pipeline
    source and scheduled task, so a red main is one incident however many
    pipelines fail, and the next green pipeline of the same kind resolves it.
    Jobs on `branch` that failed for a system reason -> `ci_job_failed`
    (project 4 only acts; see config). Everything else is ignored: script
    failures reach OpenClaw through their pipeline.
    """
    if not isinstance(payload, dict):
        raise BadPayload("payload must be an object")
    kind = payload.get("object_kind")
    if kind == "pipeline":
        attrs = payload.get("object_attributes") or {}
        project = _int((payload.get("project") or {}).get("id"))
        pid = _int(attrs.get("id"))
        if project not in projects or pid is None or attrs.get("ref") != branch:
            return []
        status = attrs.get("status")
        if status not in ("failed", "success"):
            return []
        source = str(attrs.get("source") or "")
        if not _TASK.match(source.replace("_", "-") or "x"):
            source = "other"
        task = ""
        for v in attrs.get("variables") or []:
            if isinstance(v, dict) and v.get("key") == "SCHEDULED_TASK" and _TASK.match(str(v.get("value") or "")):
                task = v["value"]
        key = f"{project}:{source}:{task or '-'}"
        labels = {"project_id": str(project), "pipeline_id": str(pid), "pipeline_source": source,
                  "pipeline_key": key}
        if task:
            labels["scheduled_task"] = task
        if status == "success":
            return [Event(source="gitlab", kind="pipeline_failed", fp=f"gitlab:pipeline:{key}",
                          status="resolved", alertname="Pipeline failed", labels=labels)]
        failed = [b for b in payload.get("builds") or []
                  if isinstance(b, dict) and b.get("status") == "failed" and not b.get("allow_failure")
                  and _JOB_NAME.match(str(b.get("name") or ""))]
        names = sorted({b["name"] for b in failed})
        labels["failed_jobs"] = ",".join(names)[:MAX_LABEL_VALUE]
        if names and all(_DEPLOY_SERVICE.match(n) for n in names):
            labels["handled_by"] = "automerge-rollback"
        reasons = sorted({str(b.get("failure_reason") or "") for b in failed} - {""})
        if reasons:
            labels["failure_reasons"] = ",".join(r for r in reasons if _TASK.match(r))[:MAX_LABEL_VALUE]
        what = f"scheduled pipeline {task}" if task else f"{source} pipeline on {branch}"
        return [Event(source="gitlab", kind="pipeline_failed", fp=f"gitlab:pipeline:{key}",
                      status="firing", alertname=f"Pipeline failed: {what}"[:120], labels=labels,
                      starts_at=str(attrs.get("finished_at") or attrs.get("created_at") or "")[:40])]
    if kind == "build":
        project = _int(payload.get("project_id"))
        job = _int(payload.get("build_id"))
        if (project not in projects or job is None or payload.get("ref") != branch
                or payload.get("build_status") != "failed" or payload.get("build_allow_failure")):
            return []
        reason = str(payload.get("build_failure_reason") or "")
        name = str(payload.get("build_name") or "")
        if reason not in SYSTEM_FAILURES or not _JOB_NAME.match(name):
            return []
        labels = {"project_id": str(project), "job_id": str(job), "job_name": name,
                  "failure_reason": reason, "failure_class": "system",
                  "pipeline_id": str(_int(payload.get("pipeline_id")) or "")}
        return [Event(source="gitlab", kind="ci_job_failed", fp=f"gitlab:job:{project}:{job}",
                      status="firing", alertname=f"CI job {name} failed ({reason})"[:120], labels=labels)]
    return []
