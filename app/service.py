"""Domain services: finalisation / sealing logic.

Sealing is the single point of unblinding. It flips the batch stage inside a
write transaction held by the caller; correctness counts are derived at read
time from immutable rows. The helper is idempotent: a batch that is already
sealed stays exactly as it was, so racing requests can never create two
different outcomes.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def seal_locked(conn: sqlite3.Connection, batch_id: str) -> bool:
    """Flip a fully-resolved, twice-submitted batch to ``sealed``.

    Caller MUST already hold a ``BEGIN IMMEDIATE`` transaction.
    Returns True when this call performed the flip, False if already sealed.
    Raises RuntimeError if a precondition is violated (internal invariant).
    """
    batch = conn.execute(
        "SELECT stage FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise KeyError(batch_id)
    if batch["stage"] == "sealed":
        return False

    unresolved = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM samples s
          LEFT JOIN classifications c1
            ON c1.batch_id = s.batch_id AND c1.code = s.code
           AND c1.reviewer = 'reviewer1'
          LEFT JOIN classifications c2
            ON c2.batch_id = s.batch_id AND c2.code = s.code
           AND c2.reviewer = 'reviewer2'
          LEFT JOIN adjudications a
            ON a.batch_id = s.batch_id AND a.code = s.code
         WHERE s.batch_id = ?
           AND (c1.label IS NULL OR c2.label IS NULL
                OR (c1.label != c2.label AND a.label IS NULL))
        """,
        (batch_id,),
    ).fetchone()["n"]
    if unresolved:
        raise RuntimeError("cannot seal with missing submissions or disputes")

    conn.execute(
        "UPDATE batches SET stage = 'sealed', sealed_at = ? WHERE id = ?",
        (utcnow(), batch_id),
    )
    return True
