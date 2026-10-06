"""SQLite connection helpers.

All writes go through ``BEGIN IMMEDIATE`` transactions so that interleaved
requests (two submits, submit/adjudicate racing) are serialised by the
database itself. WAL mode lets reads run concurrently with writes.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.environ.get("DB_PATH", "/data/blindreview.db")

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    -- accepting_submissions -> awaiting_arbitration -> sealed
    stage       TEXT NOT NULL CHECK
                (stage IN ('accepting_submissions',
                           'awaiting_arbitration',
                           'sealed')),
    categories  TEXT NOT NULL,  -- JSON array of allowed categories
    sealed_at   TEXT
);

CREATE TABLE IF NOT EXISTS credentials (
    batch_id  TEXT NOT NULL REFERENCES batches(id),
    role      TEXT NOT NULL CHECK (role IN ('reviewer1','reviewer2','arbiter')),
    token_hash TEXT NOT NULL,
    PRIMARY KEY (batch_id, role)
);

CREATE TABLE IF NOT EXISTS samples (
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    code        TEXT NOT NULL,
    true_label  TEXT NOT NULL,
    PRIMARY KEY (batch_id, code)
);

CREATE TABLE IF NOT EXISTS submissions (
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    reviewer    TEXT NOT NULL CHECK (reviewer IN ('reviewer1','reviewer2')),
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, reviewer)
);

CREATE TABLE IF NOT EXISTS classifications (
    batch_id  TEXT NOT NULL,
    reviewer  TEXT NOT NULL,
    code      TEXT NOT NULL,
    label     TEXT NOT NULL,
    PRIMARY KEY (batch_id, reviewer, code),
    FOREIGN KEY (batch_id, code) REFERENCES samples(batch_id, code)
);

CREATE TABLE IF NOT EXISTS adjudications (
    batch_id   TEXT NOT NULL,
    code       TEXT NOT NULL,
    label      TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, code),
    FOREIGN KEY (batch_id, code) REFERENCES samples(batch_id, code)
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Autocommit mode: we manage every transaction explicitly with
    # BEGIN IMMEDIATE so the write lock is taken up front.
    conn.isolation_level = None
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialising write transaction (``BEGIN IMMEDIATE``)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db(db_path: str | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
