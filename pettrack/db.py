"""SQLite schema + helpers.

Every synced row carries:
  updated_at  writer's UTC timestamp, last-write-wins
  dirty       1 = not yet pushed to the other side
  sync_seq    local monotonic counter, used as the pull cursor
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS pets(
  uid TEXT PRIMARY KEY,
  name TEXT NOT NULL, species TEXT NOT NULL, breed TEXT DEFAULT '', gender TEXT DEFAULT 'Unknown',
  birth_date TEXT DEFAULT '', color TEXT DEFAULT '', weight_kg REAL,
  allergies TEXT DEFAULT '', conditions TEXT DEFAULT '', medications TEXT DEFAULT '', notes TEXT DEFAULT '',
  owner_name TEXT NOT NULL, owner_phone TEXT NOT NULL, owner_email TEXT DEFAULT '',
  owner_city TEXT DEFAULT '', owner_address TEXT DEFAULT '', vet_name TEXT DEFAULT '',
  public_phone INTEGER DEFAULT 1, is_lost INTEGER DEFAULT 0, lost_since TEXT DEFAULT '',
  photo BLOB, photo_mime TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT, deleted INTEGER DEFAULT 0, dirty INTEGER DEFAULT 1, sync_seq INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vaccinations(
  id TEXT PRIMARY KEY, pet_uid TEXT NOT NULL, vaccine TEXT NOT NULL, given_on TEXT NOT NULL,
  next_due TEXT DEFAULT '', notes TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT, deleted INTEGER DEFAULT 0, dirty INTEGER DEFAULT 1, sync_seq INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS visits(
  id TEXT PRIMARY KEY, pet_uid TEXT NOT NULL, visited_on TEXT NOT NULL, reason TEXT DEFAULT '',
  notes TEXT DEFAULT '', vet TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT, deleted INTEGER DEFAULT 0, dirty INTEGER DEFAULT 1, sync_seq INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scans(
  id TEXT PRIMARY KEY, uid TEXT NOT NULL, source TEXT NOT NULL,
  device TEXT DEFAULT '', boot INTEGER, dev_seq INTEGER,
  occurred_at TEXT NOT NULL, time_quality TEXT DEFAULT 'live',
  created_at TEXT, updated_at TEXT, deleted INTEGER DEFAULT 0, dirty INTEGER DEFAULT 1, sync_seq INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS found_reports(
  id TEXT PRIMARY KEY, pet_uid TEXT NOT NULL, finder_name TEXT DEFAULT '', finder_phone TEXT DEFAULT '',
  message TEXT DEFAULT '', location TEXT DEFAULT '', resolved INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT, deleted INTEGER DEFAULT 0, dirty INTEGER DEFAULT 1, sync_seq INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_vacc_pet ON vaccinations(pet_uid);
CREATE INDEX IF NOT EXISTS idx_visit_pet ON visits(pet_uid);
CREATE INDEX IF NOT EXISTS idx_scan_uid ON scans(uid);
CREATE INDEX IF NOT EXISTS idx_scan_time ON scans(occurred_at);
CREATE INDEX IF NOT EXISTS idx_rep_pet ON found_reports(pet_uid);
CREATE INDEX IF NOT EXISTS idx_pets_seq ON pets(sync_seq);
CREATE INDEX IF NOT EXISTS idx_vacc_seq ON vaccinations(sync_seq);
CREATE INDEX IF NOT EXISTS idx_visit_seq ON visits(sync_seq);
CREATE INDEX IF NOT EXISTS idx_scan_seq ON scans(sync_seq);
CREATE INDEX IF NOT EXISTS idx_rep_seq ON found_reports(sync_seq);
"""

UID_RE = re.compile(r"^[0-9A-F]{4,32}$")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id() -> str:
    return uuid.uuid4().hex


def normalize_uid(raw: str | None) -> str | None:
    s = re.sub(r"[\s:\-]", "", raw or "").upper()
    return s if UID_RE.match(s) else None


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('seq', '0')")
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('instance_id', ?)", (uuid.uuid4().hex,))
        conn.commit()
    finally:
        conn.close()


def next_seq(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='seq'")
    return int(conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()[0])


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def stamp(conn: sqlite3.Connection) -> dict:
    return {"updated_at": utcnow(), "dirty": 1, "sync_seq": next_seq(conn)}


def upsert(conn: sqlite3.Connection, table: str, row: dict, pk: str) -> None:
    """Table/column names come from code constants only, never from user input."""
    cols = list(row)
    sets = ",".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    conn.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {sets}",
        [row[c] for c in cols],
    )
