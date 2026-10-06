"""Blind sample classification review HTTP API.

Roles
-----
* reviewer1 / reviewer2 : submit one complete, immutable classification pass
* arbiter               : sees only disagreed samples and picks the final
                          label from the two reviewer labels

Unblinding happens at exactly one point (``service.seal_locked``) inside a
``BEGIN IMMEDIATE`` transaction.  Until then public reads expose only sample
codes + stage, and each reviewer can only read their own submission state.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app import db as dbmod
from app.models import (
    AdjudicateIn,
    AdjudicateResult,
    BatchCreated,
    BatchIn,
    BatchView,
    DisputesOut,
    SampleResult,
    SubmitIn,
    SubmitResult,
    SubmissionStatus,
)
from app.security import compare_token, generate_token, hash_token
from app.service import seal_locked, utcnow


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    dbmod.init_db()
    yield


app = FastAPI(
    title="Blind Sample Classification Review",
    version="1.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def err(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


@app.exception_handler(sqlite3.OperationalError)
async def operational_handler(request: Request, exc: sqlite3.OperationalError):
    # Lock contention after busy_timeout: the client may safely retry.
    return JSONResponse(status_code=503, content={"detail": "database busy"})


# --------------------------------------------------------------------------- #
# DB session / authentication
# --------------------------------------------------------------------------- #
@app.middleware("http")
async def db_session(request: Request, call_next):
    """One SQLite connection per request; write ops open explicit txns."""
    request.state.conn = dbmod.connect()
    try:
        return await call_next(request)
    finally:
        request.state.conn.close()


def conn_of(request: Request) -> sqlite3.Connection:
    return request.state.conn


class Principal:
    __slots__ = ("batch_id", "role")

    def __init__(self, batch_id: str, role: str):
        self.batch_id = batch_id
        self.role = role


def require_credential(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Validate ``Authorization: Bearer <token>`` against the credential set
    bound to the batch named in the path.  Only token hashes are stored."""
    if not authorization or not authorization.startswith("Bearer "):
        raise err(401, "missing bearer credential")
    token = authorization.removeprefix("Bearer ").strip()
    batch_id = request.path_params.get("batch_id")
    if batch_id is None:
        raise err(400, "batch_id missing from path")
    conn = conn_of(request)
    rows = conn.execute(
        "SELECT role, token_hash FROM credentials WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    for row in rows:  # constant-time compare against every stored hash
        if compare_token(token, row["token_hash"]):
            return Principal(batch_id, row["role"])
    raise err(403, "invalid credential for this batch")


# --------------------------------------------------------------------------- #
# Batch creation
# --------------------------------------------------------------------------- #
@app.post("/batches", response_model=BatchCreated, status_code=201)
def create_batch(payload: BatchIn, request: Request) -> BatchCreated:
    conn = conn_of(request)
    batch_id = uuid.uuid4().hex
    categories = payload.categories
    tokens = {
        "reviewer1": generate_token(),
        "reviewer2": generate_token(),
        "arbiter": generate_token(),
    }
    now = utcnow()
    with dbmod.write_tx(conn):
        conn.execute(
            "INSERT INTO batches (id, created_at, stage, categories)"
            " VALUES (?, ?, 'accepting_submissions', ?)",
            (batch_id, now, json.dumps(categories)),
        )
        conn.executemany(
            "INSERT INTO credentials (batch_id, role, token_hash)"
            " VALUES (?, ?, ?)",
            [(batch_id, role, hash_token(tok)) for role, tok in tokens.items()],
        )
        conn.executemany(
            "INSERT INTO samples (batch_id, code, true_label) VALUES (?, ?, ?)",
            [(batch_id, s.code, s.true_label) for s in payload.samples],
        )
    # Raw credentials are emitted exactly once, here; never stored or shown
    # again afterwards.
    return BatchCreated(
        batch_id=batch_id,
        stage="accepting_submissions",
        reviewer1_token=tokens["reviewer1"],
        reviewer2_token=tokens["reviewer2"],
        arbiter_token=tokens["arbiter"],
        sample_count=len(payload.samples),
    )


# --------------------------------------------------------------------------- #
# Public batch view
# --------------------------------------------------------------------------- #
@app.get("/batches/{batch_id}", response_model=BatchView)
def get_batch(batch_id: str, request: Request) -> BatchView:
    conn = conn_of(request)
    batch = conn.execute(
        "SELECT stage, categories FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise err(404, "batch not found")

    codes = [
        r["code"]
        for r in conn.execute(
            "SELECT code FROM samples WHERE batch_id = ? ORDER BY rowid",
            (batch_id,),
        ).fetchall()
    ]

    if batch["stage"] != "sealed":
        # Before unblinding: codes + stage only.  No truth, no submit counts,
        # no classifications of either party.
        return BatchView(
            batch_id=batch_id,
            stage=batch["stage"],
            sample_codes=codes,
            sample_count=len(codes),
        )

    rows = conn.execute(
        """
        SELECT s.code      AS code,
               s.true_label AS true_label,
               c1.label     AS r1,
               c2.label     AS r2,
               a.label      AS adj
          FROM samples s
          JOIN classifications c1
            ON c1.batch_id = s.batch_id AND c1.code = s.code
           AND c1.reviewer = 'reviewer1'
          JOIN classifications c2
            ON c2.batch_id = s.batch_id AND c2.code = s.code
           AND c2.reviewer = 'reviewer2'
          LEFT JOIN adjudications a
            ON a.batch_id = s.batch_id AND a.code = s.code
         WHERE s.batch_id = ?
         ORDER BY s.rowid
        """,
        (batch_id,),
    ).fetchall()

    samples: list[SampleResult] = []
    r1_correct = r2_correct = 0
    for row in rows:
        if row["r1"] == row["r2"]:
            final_label, source = row["r1"], "agreement"
        else:
            final_label, source = row["adj"], "adjudication"
        if row["r1"] == row["true_label"]:
            r1_correct += 1
        if row["r2"] == row["true_label"]:
            r2_correct += 1
        samples.append(
            SampleResult(
                code=row["code"],
                final_label=final_label,
                true_label=row["true_label"],
                reviewer1_label=row["r1"],
                reviewer2_label=row["r2"],
                correct=final_label == row["true_label"],
                source=source,
            )
        )
    return BatchView(
        batch_id=batch_id,
        stage="sealed",
        sample_codes=codes,
        samples=samples,
        reviewer1_correct=r1_correct,
        reviewer2_correct=r2_correct,
        sample_count=len(codes),
    )


# --------------------------------------------------------------------------- #
# Reviewer endpoints
# --------------------------------------------------------------------------- #
@app.get(
    "/batches/{batch_id}/submission",
    response_model=SubmissionStatus,
)
def get_my_submission(
    batch_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_credential)],
) -> SubmissionStatus:
    conn = conn_of(request)
    if principal.role not in ("reviewer1", "reviewer2"):
        raise err(403, "only reviewers may read submission status")
    if not conn.execute(
        "SELECT 1 FROM batches WHERE id = ?", (batch_id,)
    ).fetchone():
        raise err(404, "batch not found")
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM samples WHERE batch_id = ?", (batch_id,)
    ).fetchone()["n"]
    sub = conn.execute(
        "SELECT submitted_at FROM submissions"
        " WHERE batch_id = ? AND reviewer = ?",
        (batch_id, principal.role),
    ).fetchone()
    labels: dict[str, str] | None = None
    if sub is not None:
        labels = {
            r["code"]: r["label"]
            for r in conn.execute(
                "SELECT code, label FROM classifications"
                " WHERE batch_id = ? AND reviewer = ? ORDER BY rowid",
                (batch_id, principal.role),
            ).fetchall()
        }
    return SubmissionStatus(
        batch_id=batch_id,
        reviewer=principal.role,
        submitted=sub is not None,
        submitted_at=sub["submitted_at"] if sub else None,
        sample_count=total,
        classifications=labels,
    )


@app.post(
    "/batches/{batch_id}/submission",
    response_model=SubmitResult,
    status_code=201,
)
def submit(
    batch_id: str,
    payload: SubmitIn,
    request: Request,
    principal: Annotated[Principal, Depends(require_credential)],
) -> SubmitResult:
    conn = conn_of(request)
    if principal.role not in ("reviewer1", "reviewer2"):
        raise err(403, "only reviewers may submit")

    with dbmod.write_tx(conn):
        batch = conn.execute(
            "SELECT stage, categories FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise err(404, "batch not found")
        if batch["stage"] != "accepting_submissions":
            raise err(409, "submission window is closed")

        # Immutable once handed in.  The PRIMARY KEY on (batch_id, reviewer)
        # makes even racing duplicate submits fail: at most one can win.
        already = conn.execute(
            "SELECT 1 FROM submissions WHERE batch_id = ? AND reviewer = ?",
            (batch_id, principal.role),
        ).fetchone()
        if already is not None:
            raise err(409, "already submitted; submissions cannot be changed")

        samples = {
            r["code"]: r["true_label"]
            for r in conn.execute(
                "SELECT code, true_label FROM samples WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        }
        allowed = set(json.loads(batch["categories"]))

        given = payload.classifications
        if set(given.keys()) != set(samples.keys()):
            raise err(
                422,
                "classifications must cover exactly all sample codes once",
            )
        bad = sorted({label for label in given.values() if label not in allowed})
        if bad:
            raise err(422, f"labels outside allowed categories: {bad}")

        try:
            conn.execute(
                "INSERT INTO submissions (batch_id, reviewer, submitted_at)"
                " VALUES (?, ?, ?)",
                (batch_id, principal.role, utcnow()),
            )
            conn.executemany(
                "INSERT INTO classifications (batch_id, reviewer, code, label)"
                " VALUES (?, ?, ?, ?)",
                [
                    (batch_id, principal.role, code, label)
                    for code, label in sorted(given.items())
                ],
            )
        except sqlite3.IntegrityError:
            raise err(409, "already submitted; submissions cannot be changed")

        received = conn.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()["n"]

        sealed_now = False
        stage = "accepting_submissions"
        if received == 2:
            disputes = conn.execute(
                """
                SELECT COUNT(*) AS n
                  FROM classifications a
                  JOIN classifications b
                    ON a.batch_id = b.batch_id AND a.code = b.code
                 WHERE a.batch_id = ?
                   AND a.reviewer = 'reviewer1'
                   AND b.reviewer = 'reviewer2'
                   AND a.label != b.label
                """,
                (batch_id,),
            ).fetchone()["n"]
            if disputes == 0:
                # Nothing to arbitrate: the second submission seals at once.
                sealed_now = seal_locked(conn, batch_id)
                stage = "sealed"
            else:
                conn.execute(
                    "UPDATE batches SET stage = 'awaiting_arbitration'"
                    " WHERE id = ?",
                    (batch_id,),
                )
                stage = "awaiting_arbitration"

    return SubmitResult(
        batch_id=batch_id,
        reviewer=principal.role,
        submitted=True,
        stage=stage,
        sealed_now=sealed_now,
    )


# --------------------------------------------------------------------------- #
# Arbiter endpoints
# --------------------------------------------------------------------------- #
def _load_disputes(conn: sqlite3.Connection, batch_id: str):
    # Only the two reviewer labels for disagreed samples are projected here;
    # true_label is deliberately never selected in the arbitration flow.
    return conn.execute(
        """
        SELECT s.code AS code, c1.label AS r1, c2.label AS r2,
               a.label AS adj
          FROM samples s
          JOIN classifications c1
            ON c1.batch_id = s.batch_id AND c1.code = s.code
           AND c1.reviewer = 'reviewer1'
          JOIN classifications c2
            ON c2.batch_id = s.batch_id AND c2.code = s.code
           AND c2.reviewer = 'reviewer2'
          LEFT JOIN adjudications a
            ON a.batch_id = s.batch_id AND a.code = s.code
         WHERE s.batch_id = ? AND c1.label != c2.label
         ORDER BY s.rowid
        """,
        (batch_id,),
    ).fetchall()


@app.get("/batches/{batch_id}/disputes", response_model=DisputesOut)
def get_disputes(
    batch_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(require_credential)],
) -> DisputesOut:
    conn = conn_of(request)
    if principal.role != "arbiter":
        raise err(403, "arbiter credential required")
    batch = conn.execute(
        "SELECT stage FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise err(404, "batch not found")
    if batch["stage"] != "awaiting_arbitration":
        raise err(
            409,
            "disputes are visible only while arbitration is open",
        )
    rows = _load_disputes(conn, batch_id)
    remaining = sum(1 for r in rows if r["adj"] is None)
    return DisputesOut(
        batch_id=batch_id,
        stage=batch["stage"],
        remaining=remaining,
        disputes=[
            {
                "code": r["code"],
                "reviewer1_label": r["r1"],
                "reviewer2_label": r["r2"],
                "chosen_label": r["adj"],
            }
            for r in rows
        ],
    )


@app.post(
    "/batches/{batch_id}/adjudications",
    response_model=AdjudicateResult,
    status_code=201,
)
def adjudicate(
    batch_id: str,
    payload: AdjudicateIn,
    request: Request,
    principal: Annotated[Principal, Depends(require_credential)],
) -> AdjudicateResult:
    conn = conn_of(request)
    if principal.role != "arbiter":
        raise err(403, "arbiter credential required")

    with dbmod.write_tx(conn):
        batch = conn.execute(
            "SELECT stage FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise err(404, "batch not found")
        if batch["stage"] != "awaiting_arbitration":
            raise err(409, "arbitration window is closed")

        row = conn.execute(
            """
            SELECT c1.label AS r1, c2.label AS r2, a.label AS adj
              FROM samples s
              JOIN classifications c1
                ON c1.batch_id = s.batch_id AND c1.code = s.code
               AND c1.reviewer = 'reviewer1'
              JOIN classifications c2
                ON c2.batch_id = s.batch_id AND c2.code = s.code
               AND c2.reviewer = 'reviewer2'
              LEFT JOIN adjudications a
                ON a.batch_id = s.batch_id AND a.code = s.code
             WHERE s.batch_id = ? AND s.code = ?
            """,
            (batch_id, payload.code),
        ).fetchone()
        if row is None:
            raise err(404, "sample not found in this batch")
        if row["r1"] == row["r2"]:
            raise err(422, "sample was not disputed; agreement is final")
        if row["adj"] is not None:
            # A repeated adjudication must never overwrite the prior choice.
            raise err(409, "already adjudicated; the choice cannot be changed")

        choices = {row["r1"], row["r2"]}
        if payload.label not in choices:
            raise err(
                422,
                f"final label must be one of the reviewer labels: {sorted(choices)}",
            )

        try:
            conn.execute(
                "INSERT INTO adjudications (batch_id, code, label, decided_at)"
                " VALUES (?, ?, ?, ?)",
                (batch_id, payload.code, payload.label, utcnow()),
            )
        except sqlite3.IntegrityError:
            raise err(409, "already adjudicated; the choice cannot be changed")

        remaining = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM samples s
              JOIN classifications c1
                ON c1.batch_id = s.batch_id AND c1.code = s.code
               AND c1.reviewer = 'reviewer1'
              JOIN classifications c2
                ON c2.batch_id = s.batch_id AND c2.code = s.code
               AND c2.reviewer = 'reviewer2'
              LEFT JOIN adjudications a
                ON a.batch_id = s.batch_id AND a.code = s.code
             WHERE s.batch_id = ? AND c1.label != c2.label AND a.label IS NULL
            """,
            (batch_id,),
        ).fetchone()["n"]

        sealed_now = False
        stage = "awaiting_arbitration"
        if remaining == 0:
            # The final outstanding decision triggers one atomic unblinding.
            sealed_now = seal_locked(conn, batch_id)
            stage = "sealed"

    return AdjudicateResult(
        batch_id=batch_id,
        code=payload.code,
        chosen_label=payload.label,
        remaining=remaining,
        stage=stage,
        sealed_now=sealed_now,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
