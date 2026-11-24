import hashlib
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

DB = Path(os.environ.get("LAUNDRY_DB", Path(__file__).with_name("laundry.db")))
DAY = 86400
SESSION_TTL = 90 * DAY
SHARE_TTL = 30 * DAY
OWNER_CODE_TTL = 7 * DAY
SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
    sensor_id TEXT PRIMARY KEY, token TEXT NOT NULL, code TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0,
    monitoring INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'idle',
    last_at REAL, started_at REAL, session_id TEXT, boot TEXT, sequence INTEGER,
    low INTEGER, high INTEGER, phase TEXT, phase_at REAL, last_brightness INTEGER,
    code_expires REAL NOT NULL, owner_code TEXT, owner_code_expires REAL
);
CREATE TABLE IF NOT EXISTS boots (
    sensor_id TEXT REFERENCES sensors ON DELETE CASCADE, boot TEXT, PRIMARY KEY(sensor_id, boot)
);
CREATE TABLE IF NOT EXISTS phones (
    installation_id TEXT PRIMARY KEY, sensor_id TEXT NOT NULL REFERENCES sensors ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 0,
    subscription TEXT, endpoint TEXT UNIQUE, last_test REAL, expires_at REAL NOT NULL,
    controller INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY, sensor_id TEXT NOT NULL REFERENCES sensors ON DELETE CASCADE,
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT
);
CREATE INDEX IF NOT EXISTS sessions_sensor_time ON sessions(sensor_id, started_at DESC);
CREATE TABLE IF NOT EXISTS outbox (
    id TEXT PRIMARY KEY, phone TEXT NOT NULL REFERENCES phones ON DELETE CASCADE,
    kind TEXT NOT NULL, expires REAL NOT NULL, next_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS push_test_events (
    sensor_id TEXT NOT NULL REFERENCES sensors ON DELETE CASCADE, requested_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS push_test_time ON push_test_events(requested_at);
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def initialize(path, now=None):
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {column[1] for column in db.execute("PRAGMA table_info(sensors)")}
            if "last_brightness" not in columns:
                db.execute("ALTER TABLE sensors ADD COLUMN last_brightness INTEGER")
                # Discard reference levels learned from individual samples by the old detector.
                db.execute("UPDATE sensors SET low=NULL, high=NULL")
            if "code_expires" not in columns:
                db.execute("ALTER TABLE sensors ADD COLUMN code_expires REAL NOT NULL DEFAULT 0")
                db.execute("ALTER TABLE sensors ADD COLUMN owner_code TEXT")
                db.execute("ALTER TABLE sensors ADD COLUMN owner_code_expires REAL")
                db.execute("UPDATE sensors SET code_expires=?", (now + SHARE_TTL,))
            phone_columns = {column[1] for column in db.execute("PRAGMA table_info(phones)")}
            if "expires_at" not in phone_columns:
                db.execute("ALTER TABLE phones ADD COLUMN expires_at REAL NOT NULL DEFAULT 0")
                db.execute("UPDATE phones SET expires_at=?", (now + SESSION_TTL,))
            if "controller" not in phone_columns:
                db.execute("ALTER TABLE phones ADD COLUMN controller INTEGER NOT NULL DEFAULT 0")
                db.execute("""UPDATE phones SET controller=1 WHERE rowid IN
                    (SELECT MIN(rowid) FROM phones WHERE expires_at>? GROUP BY sensor_id)""", (now,))
                db.execute("UPDATE phones SET expires_at=MIN(expires_at, ?)", (now + SESSION_TTL,))
            db.execute("CREATE INDEX IF NOT EXISTS phones_expiry ON phones(expires_at)")


@contextmanager
def transaction(path, *, write=True):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if not write:
        db.execute("PRAGMA query_only=ON")
    try:
        with db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
    finally:
        db.close()


def queue(db, phone, kind, now):
    db.execute("INSERT INTO outbox VALUES (?, ?, ?, ?, ?)",
               (uuid4().hex, phone, kind, now + 600, now))


def broadcast(db, sensor_id, kind, now):
    if kind in ("offline", "recovered"):
        db.execute("""DELETE FROM outbox WHERE kind IN ('offline', 'recovered')
            AND phone IN (SELECT installation_id FROM phones WHERE sensor_id=?)""", (sensor_id,))
    phones = db.execute(
        """SELECT installation_id FROM phones
            WHERE sensor_id=? AND enabled=1 AND subscription IS NOT NULL AND expires_at>?""",
        (sensor_id, now),
    ).fetchall()
    for phone in phones:
        queue(db, phone["installation_id"], kind, now)


def close_session(db, session_id, reason, now):
    db.execute("UPDATE sessions SET ended_at=?, end_reason=? WHERE session_id=?",
               (now, reason, session_id))


def expire_sensor(db, device, now):
    if device["state"] == "running" and now - device["last_at"] >= 30:
        db.execute("UPDATE sensors SET state='error', phase=NULL, phase_at=NULL WHERE sensor_id=?",
                   (device["sensor_id"],))
        broadcast(db, device["sensor_id"], "offline", now)
        return True
    return False


def reading(db, device, sample, now, min_delta):
    sensor_id = device["sensor_id"]
    previous = device["last_at"]
    rebooted = sample.boot_id != device["boot"]
    if not rebooted and sample.sequence <= device["sequence"]:
        return
    if rebooted:
        result = db.execute("INSERT OR IGNORE INTO boots VALUES (?, ?)", (sensor_id, sample.boot_id))
        if not result.rowcount:
            return

    state, session, started = device["state"], device["session_id"], device["started_at"]
    phase, phase_at = device["phase"], device["phase_at"]
    if expire_sensor(db, device, now) or state == "error":
        state = "running"
        broadcast(db, sensor_id, "recovered", now)
    continuous = (
        not rebooted and previous is not None and 0 <= now - previous <= 8
        and sample.sequence == device["sequence"] + 1
    )
    if not continuous:
        phase, phase_at = None, None

    low, high = device["low"], device["high"]
    value = sample.brightness
    last_value = device["last_brightness"]
    stable_pair = continuous and last_value is not None and 2 * abs(value - last_value) <= min_delta
    if device["monitoring"]:
        # Learn only from consecutive readings at the same level, and freeze the
        # references during a session so the OFF threshold stays consistent.
        if state != "running" and stable_pair:
            reference = (last_value + value) // 2
            low = reference if low is None else min(low, reference)
            high = reference if high is None else max(high, reference)
        level = None
        on_threshold = None
        if low is not None and high is not None and high - low >= min_delta:
            on_threshold = low + (high - low) * .7
            if value >= on_threshold:
                level = "on"
            elif value <= low + (high - low) * .3:
                level = "off"
        expected = "off" if state == "running" else "on"
        if level != expected:
            phase, phase_at = None, None
        elif phase != expected:
            phase, phase_at = expected, now
            if expected == "on" and stable_pair and last_value >= on_threshold:
                phase_at = previous
        elif expected == "on" and now - phase_at >= 10:
            state, session, started = "running", uuid4().hex, now
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, NULL, NULL)", (session, sensor_id, now))
            phase, phase_at = None, None
        elif expected == "off" and now - phase_at >= 30:
            close_session(db, session, "completed", now)
            broadcast(db, sensor_id, "completed", now)
            state, session, started = "completed", None, None
            phase, phase_at = None, None

    db.execute("""UPDATE sensors SET state=?, last_at=?, started_at=?, session_id=?, boot=?,
        sequence=?, low=?, high=?, phase=?, phase_at=?, last_brightness=? WHERE sensor_id=?""",
        (state, now, started, session, sample.boot_id, sample.sequence, low, high, phase, phase_at,
         value if device["monitoring"] else None, sensor_id))
