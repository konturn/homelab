"""Minimal InfluxDB 2.x HTTP client (stdlib only): Flux queries as CSV, and
line-protocol writes."""
import csv
import datetime as dt
import io
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)


class InfluxError(RuntimeError):
    pass


def parse_time(v):
    if not v:
        return None
    v = v.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # Python <3.11 fromisoformat cannot take 9 fractional digits; trim to 6.
    if "." in v:
        head, rest = v.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        v = f"{head}.{frac[:6]}{tz}"
    try:
        return dt.datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


def parse_csv(text):
    """Annotated-less Flux CSV (header=true) -> list of row dicts.

    Each table starts with its own header row; blank lines separate results.
    """
    rows, header = [], None
    for rec in csv.reader(io.StringIO(text)):
        if not rec or all(c == "" for c in rec):
            header = None
            continue
        if header is None or (len(rec) > 2 and rec[1] == "result" and rec[2] == "table"):
            header = rec
            continue
        row = {k: v for k, v in zip(header, rec) if k and k not in ("result", "table")}
        if "error" in header and row.get("error"):
            raise InfluxError(row["error"])
        rows.append(row)
    return rows


def to_float(v):
    if v is None or v == "":
        return None
    if v in ("true", "false"):
        return 1.0 if v == "true" else 0.0
    try:
        return float(v)
    except ValueError:
        return None


class Influx:
    def __init__(self, url, org, read_token, write_token=None, timeout=30):
        self.url = url.rstrip("/")
        self.org = org
        self.read_token = read_token
        self.write_token = write_token
        self.timeout = timeout
        self.queries = 0
        self.query_seconds = 0.0
        self.last_error = None

    def _req(self, path, token, body, content_type, accept=None):
        req = urllib.request.Request(self.url + path, data=body, method="POST")
        req.add_header("Authorization", f"Token {token}")
        req.add_header("Content-Type", content_type)
        if accept:
            req.add_header("Accept", accept)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise InfluxError(f"HTTP {e.code} on {path.split('?')[0]}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise InfluxError(f"{path.split('?')[0]}: {e}") from None

    def query(self, flux):
        t0 = time.monotonic()
        body = json.dumps({"query": flux, "type": "flux",
                           "dialect": {"header": True, "annotations": [], "delimiter": ","}}).encode()
        try:
            text = self._req("/api/v2/query?" + urllib.parse.urlencode({"org": self.org}),
                             self.read_token, body, "application/json", "application/csv")
            self.last_error = None
            return parse_csv(text)
        except InfluxError as e:
            self.last_error = str(e)
            raise
        finally:
            self.queries += 1
            self.query_seconds += time.monotonic() - t0

    def write(self, bucket, lines):
        if not lines:
            return
        if not self.write_token:
            raise InfluxError("no write token configured")
        qs = urllib.parse.urlencode({"org": self.org, "bucket": bucket, "precision": "s"})
        self._req("/api/v2/write?" + qs, self.write_token, "\n".join(lines).encode(),
                  "text/plain; charset=utf-8")

    def ping(self):
        req = urllib.request.Request(self.url + "/health")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get("status") == "pass"


# ---------------------------------------------------------------------------
# Line protocol
# ---------------------------------------------------------------------------
def _esc_key(s):
    return str(s).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _field(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def line(measurement, tags, fields, epoch):
    tag_s = "".join(f",{_esc_key(k)}={_esc_key(v if v != '' else '-')}"
                    for k, v in sorted(tags.items()) if v is not None)
    fields = {k: v for k, v in fields.items() if v is not None
              and not (isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))))}
    field_s = ",".join(f"{_esc_key(k)}={_field(v)}" for k, v in fields.items())
    return f"{_esc_key(measurement)}{tag_s} {field_s} {int(epoch)}"
