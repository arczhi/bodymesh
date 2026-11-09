"""SQLite storage: one file, one graph, no server to babysit.

Design notes
------------
* One wide ``daily`` row per calendar day (local time). Rolling baselines are
  recomputed from it, so a late arrival of yesterday's data never needs a
  migration.
* ``activities`` and ``meals`` are event tables; ``body`` keeps weigh-ins.
* ``insights`` is the *derived* layer: deterministic findings with evidence,
  written by :mod:`bodymesh.insights` and read by the agent. Keeping findings
  out of the language model's head is what makes them reproducible.
* ``briefs`` stores what was actually sent, so the morning message has an audit
  trail.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS daily (
    date              TEXT PRIMARY KEY,
    rhr               REAL,
    rhr_7d            REAL,
    hrv_ms            REAL,
    hrv_status        TEXT,
    hrv_base_low      REAL,
    hrv_base_high     REAL,
    sleep_s           INTEGER,
    sleep_score       INTEGER,
    sleep_qualifier   TEXT,
    deep_s            INTEGER,
    rem_s             INTEGER,
    awake_s           INTEGER,
    sleep_stress      REAL,
    sleep_hr          REAL,
    resp_sleep        REAL,
    resp_waking       REAL,
    sleep_feedback    TEXT,
    stress_avg        REAL,
    stress_max        REAL,
    bb_high           INTEGER,
    bb_low            INTEGER,
    bb_charged        INTEGER,
    bb_drained        INTEGER,
    steps             INTEGER,
    total_kcal        REAL,
    active_kcal       REAL,
    bmr_kcal          REAL,
    intensity_min     INTEGER,
    active_s          INTEGER,
    highly_active_s   INTEGER,
    vo2max            REAL,
    training_status   TEXT,
    acute_load        REAL,
    chronic_load      REAL,
    load_ratio        REAL,
    acwr_status       TEXT,
    weight_kg         REAL,
    body_fat_pct      REAL,
    muscle_kg         REAL,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    id           TEXT PRIMARY KEY,
    date         TEXT NOT NULL,
    type         TEXT,
    name         TEXT,
    start_local  TEXT,
    duration_s   REAL,
    moving_s     REAL,
    distance_m   REAL,
    avg_hr       REAL,
    max_hr       REAL,
    calories     REAL,
    te_aerobic   REAL,
    te_anaerobic REAL,
    training_load REAL,
    cadence      REAL,
    gct_ms       REAL,
    vert_osc_cm  REAL,
    stride_cm    REAL,
    elev_gain_m  REAL,
    power_avg    REAL,
    power_norm   REAL,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

CREATE TABLE IF NOT EXISTS meals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    date        TEXT NOT NULL,
    slot        TEXT,
    description TEXT NOT NULL,
    items       TEXT,
    kcal        REAL,
    protein_g   REAL,
    carbs_g     REAL,
    fat_g       REAL,
    fiber_g     REAL,
    source      TEXT,
    confidence  REAL,
    image_path  TEXT,
    notes       TEXT,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(date);

CREATE TABLE IF NOT EXISTS body (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    date         TEXT NOT NULL,
    weight_kg    REAL,
    body_fat_pct REAL,
    muscle_kg    REAL,
    water_pct    REAL,
    source       TEXT,
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_body_date ON body(date);

CREATE TABLE IF NOT EXISTS notes (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts   TEXT NOT NULL,
    date TEXT NOT NULL,
    kind TEXT,
    text TEXT NOT NULL,
    tags TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_date ON notes(date);

CREATE TABLE IF NOT EXISTS insights (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    date       TEXT NOT NULL,
    code       TEXT NOT NULL,
    severity   TEXT,
    title      TEXT,
    detail     TEXT,
    evidence   TEXT,
    created_at TEXT,
    UNIQUE(date, code) ON CONFLICT REPLACE
);

CREATE TABLE IF NOT EXISTS briefs (
    date         TEXT PRIMARY KEY,
    markdown     TEXT,
    generated_at TEXT,
    model        TEXT,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and if needed create) the node's database."""
    target = Path(path) if path else config.db_path()
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path: Path | str | None = None) -> Path:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    return Path(path) if path else config.db_path()


def ensure_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def upsert(conn: sqlite3.Connection, table: str, row: dict[str, Any], key: str = "date") -> None:
    """Insert-or-update a row, ignoring unknown columns."""
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != key)
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


def insert(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> int:
    cols = list(row)
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})"
    cur = conn.execute(sql, [row[c] for c in cols])
    return int(cur.lastrowid or 0)


def one(conn: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


def rows(conn: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, args: Sequence[Any] = ()) -> Any:
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else None


def daily(conn: sqlite3.Connection, date: str) -> dict[str, Any]:
    return one(conn, "SELECT * FROM daily WHERE date=?", (date,)) or {"date": date}


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    raw = scalar(conn, "SELECT value FROM meta WHERE key=?", (key,))
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def date_range(start: str, end: str) -> list[str]:
    """Inclusive list of ISO dates between ``start`` and ``end``."""
    a = date_cls.fromisoformat(start)
    b = date_cls.fromisoformat(end)
    if b < a:
        a, b = b, a
    out: list[str] = []
    cur = a
    while cur <= b:
        out.append(cur.isoformat())
        cur = date_cls.fromordinal(cur.toordinal() + 1)
    return out


def upsert_many(conn: sqlite3.Connection, table: str, records: Iterable[dict[str, Any]], key: str = "date") -> int:
    n = 0
    for rec in records:
        upsert(conn, table, rec, key=key)
        n += 1
    return n
