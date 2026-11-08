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
