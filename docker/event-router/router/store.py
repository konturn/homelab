"""Incident state in SQLite (section 4.6): survives restarts (S8).

One connection per thread; WAL so the HTTP thread can append to the inbox
while the worker decides. The database is private to the container: what the
host services need is published as small files (router/shared.py).
"""

import json
import sqlite3
import threading
import uuid

ACTIVE = ("open", "symptom", "acting", "verifying", "maint")
TERMINAL = ("resolved", "escalated", "paged", "shadowed", "suppressed", "digest")
# Terminal states whose alert may still be firing: the root cause is still
# open for correlation (doc 12.6 e). Everything terminal except "resolved".
HANDED_OFF = ("escalated", "paged", "shadowed", "suppressed", "digest")

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, received_at REAL NOT NULL, event TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY, fp TEXT NOT NULL, source TEXT, kind TEXT, rule_uid TEXT,
  alertname TEXT, node TEXT, labels TEXT, status TEXT NOT NULL, playbook TEXT,
  target TEXT, alert_state TEXT NOT NULL, page_class TEXT,
  opened_at REAL NOT NULL, updated_at REAL NOT NULL, closed_at REAL,
  deliveries INTEGER NOT NULL DEFAULT 1, exec_count INTEGER NOT NULL DEFAULT 0,
  exec_started_at REAL, verify_deadline REAL, root TEXT, waiting TEXT, note TEXT,
  summary TEXT, verify_ref TEXT);
CREATE INDEX IF NOT EXISTS incidents_fp ON incidents(fp, opened_at);
CREATE INDEX IF NOT EXISTS incidents_status ON incidents(status);
CREATE TABLE IF NOT EXISTS locks (
  target TEXT PRIMARY KEY, incident TEXT NOT NULL, since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, playbook TEXT NOT NULL, target TEXT,
  mode TEXT NOT NULL, ts REAL NOT NULL, incident TEXT);
CREATE TABLE IF NOT EXISTS breakers (
  playbook TEXT NOT NULL, mode TEXT NOT NULL, opened_at REAL NOT NULL,
  last_trigger REAL NOT NULL, PRIMARY KEY (playbook, mode));
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident TEXT NOT NULL, playbook TEXT,
  action TEXT, target TEXT, ts REAL NOT NULL, result TEXT);
CREATE TABLE IF NOT EXISTS windows (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, mode TEXT NOT NULL,
  playbooks TEXT NOT NULL DEFAULT '[]', page_classes TEXT NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL, opened_by TEXT, opened_at REAL NOT NULL,
  expires_at REAL NOT NULL, closed_at REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, incident TEXT,
  decision TEXT NOT NULL, playbook TEXT, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS forwards (
  id INTEGER PRIMARY KEY AUTOINCREMENT, incident TEXT NOT NULL, kind TEXT NOT NULL,
  urgent INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, due_at REAL NOT NULL,
  next_try REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  state TEXT NOT NULL DEFAULT 'pending', delivered_at REAL, jit_at REAL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS forwards_state ON forwards(state, due_at);
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, incident TEXT, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen (
  fp TEXT NOT NULL, status TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (fp, status));
"""

_INC_JSON = ("labels",)


class Store:
    def __init__(self, path, durable=True):
        self.path = path
        self.durable = durable     # tests turn fsync off
        self._local = threading.local()
        self.conn.executescript(SCHEMA)
        self._migrate()

    # Columns added after a database was created (phase 3). SQLite has no
    # ADD COLUMN IF NOT EXISTS.
    MIGRATIONS = {"incidents": {"summary": "TEXT", "verify_ref": "TEXT"}}

    def _migrate(self):
        for table, cols in self.MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=" + ("FULL" if self.durable else "OFF"))
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def tx(self):
        return _Tx(self.conn)

    # ---- inbox ---------------------------------------------------------
    def inbox_put(self, now, event_dicts):
        """Accept events (written before the HTTP reply), and remember the
        fingerprints for /ack: OpenClaw's Grafana route asks whether the
        router has an alert before it stays out of the way (doc 7.4, L4)."""
        with self.tx() as c:
            for e in event_dicts:
                c.execute("INSERT INTO inbox (received_at, event) VALUES (?, ?)",
                          (now, json.dumps(e, sort_keys=True)))
                c.execute("INSERT INTO seen (fp, status, ts) VALUES (?, ?, ?) "
                          "ON CONFLICT (fp, status) DO UPDATE SET ts = excluded.ts",
                          (e["fp"], e["status"], now))

    def seen_since(self, fp, status, since):
        r = self.conn.execute("SELECT ts FROM seen WHERE fp = ? AND status = ? AND ts >= ?",
                              (fp, status, since)).fetchone()
        return r is not None

    def prune_seen(self, before):
        self.conn.execute("DELETE FROM seen WHERE ts < ?", (before,))

    def inbox_take(self, limit=100):
        rows = self.conn.execute(
            "SELECT id, event FROM inbox ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [(r["id"], json.loads(r["event"])) for r in rows]

    def inbox_done(self, row_id):
        self.conn.execute("DELETE FROM inbox WHERE id = ?", (row_id,))

    # ---- incidents -----------------------------------------------------
    @staticmethod
    def _inc(row):
        if row is None:
            return None
        d = dict(row)
        for k in _INC_JSON:
            d[k] = json.loads(d[k] or "{}")
        return d

    def new_incident(self, now, **fields):
        inc_id = "inc-" + uuid.uuid4().hex[:12]
        fields = dict(fields, id=inc_id, opened_at=now, updated_at=now)
        fields["labels"] = json.dumps(fields.get("labels") or {}, sort_keys=True)
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        self.conn.execute(f"INSERT INTO incidents ({cols}) VALUES ({marks})", tuple(fields.values()))
        return inc_id

    def get(self, inc_id):
        return self._inc(self.conn.execute("SELECT * FROM incidents WHERE id = ?", (inc_id,)).fetchone())

    def latest_for_fp(self, fp):
        return self._inc(self.conn.execute(
            "SELECT * FROM incidents WHERE fp = ? ORDER BY opened_at DESC, rowid DESC LIMIT 1",
            (fp,)).fetchone())

    def active(self):
        marks = ",".join("?" for _ in ACTIVE)
        rows = self.conn.execute(
            f"SELECT * FROM incidents WHERE status IN ({marks}) ORDER BY opened_at, rowid",
            ACTIVE).fetchall()
        return [self._inc(r) for r in rows]

    def handed_off_firing(self, since):
        """Incidents handed to OpenClaw or Noah (or shadowed) whose alert
        still fires: their root cause is still open for correlation."""
        marks = ",".join("?" for _ in HANDED_OFF)
        rows = self.conn.execute(
            f"SELECT * FROM incidents WHERE status IN ({marks}) "
            "AND alert_state = 'firing' AND opened_at >= ? ORDER BY opened_at", (*HANDED_OFF, since)).fetchall()
        return [self._inc(r) for r in rows]

    def update(self, inc_id, now, **fields):
        fields["updated_at"] = now
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE incidents SET {sets} WHERE id = ?", (*fields.values(), inc_id))

    def recent_incidents(self, since):
        rows = self.conn.execute(
            "SELECT * FROM incidents WHERE opened_at >= ? ORDER BY opened_at", (since,)).fetchall()
        return [self._inc(r) for r in rows]

    # ---- locks ---------------------------------------------------------
    def take_lock(self, target, inc_id, now):
        try:
            self.conn.execute("INSERT INTO locks (target, incident, since) VALUES (?, ?, ?)",
                              (target, inc_id, now))
            return True
        except sqlite3.IntegrityError:
            return False

    def lock_holder(self, target):
        r = self.conn.execute("SELECT incident FROM locks WHERE target = ?", (target,)).fetchone()
        return r["incident"] if r else None

    def release_lock(self, target, inc_id):
        self.conn.execute("DELETE FROM locks WHERE target = ? AND incident = ?", (target, inc_id))

    def locks(self):
        return {r["target"]: r["incident"] for r in self.conn.execute("SELECT * FROM locks")}

    # ---- rate limits and breakers ------------------------------------------
    def add_run(self, playbook, target, mode, now, inc_id):
        self.conn.execute("INSERT INTO runs (playbook, target, mode, ts, incident) VALUES (?, ?, ?, ?, ?)",
                          (playbook, target, mode, now, inc_id))

    def runs_since(self, playbook, target, mode, since):
        return [r["ts"] for r in self.conn.execute(
            "SELECT ts FROM runs WHERE playbook = ? AND target = ? AND mode = ? AND ts >= ? ORDER BY ts",
            (playbook, target, mode, since))]

    def active_runs_since(self, since):
        return self.conn.execute("SELECT COUNT(*) FROM runs WHERE mode = 'active' AND ts >= ?",
                                 (since,)).fetchone()[0]

    def breaker(self, playbook, mode):
        r = self.conn.execute("SELECT * FROM breakers WHERE playbook = ? AND mode = ?",
                              (playbook, mode)).fetchone()
        return dict(r) if r else None

    def open_breaker(self, playbook, mode, now):
        self.conn.execute(
            "INSERT INTO breakers (playbook, mode, opened_at, last_trigger) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (playbook, mode) DO UPDATE SET last_trigger = excluded.last_trigger",
            (playbook, mode, now, now))

    def reset_breaker(self, playbook, mode):
        self.conn.execute("DELETE FROM breakers WHERE playbook = ? AND mode = ?", (playbook, mode))

    def breakers(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM breakers")]

    def add_action(self, inc_id, playbook, action, target, now, result):
        self.conn.execute(
            "INSERT INTO actions (incident, playbook, action, target, ts, result) VALUES (?, ?, ?, ?, ?, ?)",
            (inc_id, playbook, action, target, now, result))

    def actions_since(self, since):
        return self.conn.execute("SELECT COUNT(*) FROM actions WHERE ts >= ?", (since,)).fetchone()[0]

    # ---- maintenance windows ---------------------------------------------
    def open_window(self, now, scope, mode, reason, expires_at, opened_by, playbooks=(), page_classes=()):
        wid = "maint-" + uuid.uuid4().hex[:8]
        self.conn.execute(
            "INSERT INTO windows (id, scope, mode, playbooks, page_classes, reason, opened_by, opened_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (wid, json.dumps(sorted(scope)), mode, json.dumps(sorted(playbooks)),
             json.dumps(sorted(page_classes)), reason, opened_by, now, expires_at))
        return wid

    def windows(self, include_closed=False):
        q = "SELECT * FROM windows" + ("" if include_closed else " WHERE closed_at IS NULL")
        out = []
        for r in self.conn.execute(q + " ORDER BY opened_at"):
            d = dict(r)
            for k in ("scope", "playbooks", "page_classes"):
                d[k] = json.loads(d[k])
            out.append(d)
        return out

    def close_window(self, wid, now):
        self.conn.execute("UPDATE windows SET closed_at = ? WHERE id = ? AND closed_at IS NULL", (now, wid))

    # ---- settings ------------------------------------------------------
    def setting(self, key, default=None):
        r = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_setting(self, key, value):
        if value is None:
            self.conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, sort_keys=True)))

    # ---- decisions and notifications -------------------------------------------
    def add_decision(self, now, record):
        self.conn.execute(
            "INSERT INTO decisions (ts, incident, decision, playbook, record) VALUES (?, ?, ?, ?, ?)",
            (now, record.get("incident"), record["decision"], record.get("playbook"),
             json.dumps(record, sort_keys=True)))

    def decisions_since(self, since):
        return [json.loads(r["record"]) for r in self.conn.execute(
            "SELECT record FROM decisions WHERE ts >= ? ORDER BY id", (since,))]


    # ---- forwards to OpenClaw (phase 3) --------------------------------------
    def add_forward(self, inc_id, kind, urgent, now, due_at, payload):
        cur = self.conn.execute(
            "INSERT INTO forwards (incident, kind, urgent, created, due_at, next_try, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (inc_id, kind, int(bool(urgent)), now, due_at, due_at, json.dumps(payload, sort_keys=True)))
        return cur.lastrowid

    @staticmethod
    def _fwd(row):
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["urgent"] = bool(d["urgent"])
        return d

    def pending_forwards(self):
        return [self._fwd(r) for r in self.conn.execute(
            "SELECT * FROM forwards WHERE state = 'pending' ORDER BY id")]

    def forward_for(self, inc_id):
        r = self.conn.execute("SELECT * FROM forwards WHERE incident = ? ORDER BY id DESC LIMIT 1",
                              (inc_id,)).fetchone()
        return self._fwd(r) if r else None

    def update_forward(self, fid, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE forwards SET {sets} WHERE id = ?", (*fields.values(), fid))

    def forwards_since(self, since):
        return [self._fwd(r) for r in self.conn.execute(
            "SELECT * FROM forwards WHERE created >= ? ORDER BY id", (since,))]

    # ---- outcome records (8.2) ---------------------------------------------
    def add_outcome(self, now, rec):
        self.conn.execute("INSERT INTO outcomes (ts, incident, record) VALUES (?, ?, ?)",
                          (now, rec.get("incident"), json.dumps(rec, sort_keys=True)))

    def outcomes_since(self, since):
        return [dict(json.loads(r["record"]), ts=r["ts"]) for r in self.conn.execute(
            "SELECT ts, record FROM outcomes WHERE ts >= ? ORDER BY id", (since,))]

    def outcome_for(self, inc_id):
        r = self.conn.execute("SELECT ts, record FROM outcomes WHERE incident = ? ORDER BY id DESC LIMIT 1",
                              (inc_id,)).fetchone()
        return dict(json.loads(r["record"]), ts=r["ts"]) if r else None

    def decisions_for(self, inc_id, limit=50):
        return [json.loads(r["record"]) for r in self.conn.execute(
            "SELECT record FROM decisions WHERE incident = ? ORDER BY id DESC LIMIT ?", (inc_id, limit))][::-1]

    def active_by_label(self, kind, label, value):
        """Incidents of a kind with label == value whose alert still fires."""
        rows = self.conn.execute(
            "SELECT * FROM incidents WHERE kind = ? AND alert_state = 'firing' ORDER BY opened_at",
            (kind,)).fetchall()
        return [i for i in (self._inc(r) for r in rows) if i["labels"].get(label) == value]


class _Tx:
    def __init__(self, conn):
        self.c = conn

    def __enter__(self):
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, *_):
        self.c.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
