"""DuckDB-backed storage layer. All writes are inserts; there is no
UPDATE, DELETE or DROP anywhere in this module or the rest of the
package (a test scans package source for this)."""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .model import (
    EDGE_LABELS,
    Edge,
    Event,
    EvidenceRow,
    OPENING_KINDS,
    TimeMark,
    ValidationError,
    compute_event_id,
    compute_time_mark_id,
    parse_any_timestamp,
    parse_strict_iso8601,
    utcnow,
    validate_content,
    validate_kind,
)
from .reference import ViewSelector, compute_closers, compute_live, compute_status_all

SCHEMA_SQL = """
SET TimeZone='UTC';

CREATE SEQUENCE IF NOT EXISTS ev_seq START 1;

CREATE TABLE IF NOT EXISTS events (
  seq BIGINT PRIMARY KEY DEFAULT nextval('ev_seq'),
  id VARCHAR UNIQUE NOT NULL,
  kind VARCHAR NOT NULL,
  content VARCHAR NOT NULL,
  actor VARCHAR NOT NULL,
  occurred_at TIMESTAMP NOT NULL,
  recorded_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
  src VARCHAR NOT NULL,
  dst VARCHAR NOT NULL,
  label VARCHAR NOT NULL,
  PRIMARY KEY (src, dst, label),
  CHECK (label IN ('from','needs','fulfils','drops','retracts')),
  CHECK (src <> dst)
);

CREATE TABLE IF NOT EXISTS blobs (
  hash VARCHAR PRIMARY KEY,
  media_type VARCHAR NOT NULL,
  size BIGINT NOT NULL,
  recorded_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS blob_locations (
  hash VARCHAR NOT NULL,
  uri VARCHAR NOT NULL,
  recorded_at TIMESTAMP NOT NULL,
  PRIMARY KEY (hash, uri, recorded_at)
);

CREATE TABLE IF NOT EXISTS evidence (
  seq BIGINT PRIMARY KEY DEFAULT nextval('ev_seq'),
  id VARCHAR UNIQUE NOT NULL,
  event_id VARCHAR NOT NULL,
  blob_hash VARCHAR NOT NULL,
  note VARCHAR NOT NULL DEFAULT '',
  actor VARCHAR NOT NULL,
  recorded_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS time_marks (
  seq BIGINT PRIMARY KEY DEFAULT nextval('ev_seq'),
  id VARCHAR UNIQUE NOT NULL,
  event_id VARCHAR NOT NULL,
  at_time TIMESTAMP NOT NULL,
  note VARCHAR NOT NULL DEFAULT '',
  actor VARCHAR NOT NULL,
  recorded_at TIMESTAMP NOT NULL
);
"""

LEDGER_DIR_NAME = ".ledger"
LEDGER_FILE_NAME = "ledger.duckdb"


class LedgerBusyError(RuntimeError):
    """Raised when the DuckDB lock cannot be acquired within budget."""


class NotFoundError(LookupError):
    pass


class AmbiguousIdError(LookupError):
    pass


# --- Path discovery -----------------------------------------------------------


def discover_db_path(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) like git, looking for
    .ledger/ledger.duckdb. Returns None if not found."""
    cur = (start or Path.cwd()).resolve()
    while True:
        candidate = cur / LEDGER_DIR_NAME / LEDGER_FILE_NAME
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def resolve_db_path(explicit: str | None) -> Path | None:
    """Resolve the ledger path the way every command but `init` should:
    explicit --db, then EV_DB, then upward discovery from cwd (like
    git). Returns None if none of those found anything — callers must
    NOT silently fall back to creating a ledger in cwd; only `ev init`
    (and explicit --db/EV_DB, which are explicit user intent) may
    bring a new ledger file into existence."""
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("EV_DB")
    if env:
        return Path(env).resolve()
    return discover_db_path()


def init_ledger(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    ledger_dir = base / LEDGER_DIR_NAME
    ledger_dir.mkdir(parents=True, exist_ok=True)
    db_path = ledger_dir / LEDGER_FILE_NAME
    con = connect(db_path)
    con.close()
    return db_path


# --- Connection -----------------------------------------------------------


def _retry_on_lock(open_fn, retry_seconds: float):
    """Call open_fn() (which must itself open+prepare a connection),
    retrying for up to retry_seconds on a lock conflict, then raising
    LedgerBusyError. DuckDB allows one writer (and no concurrent
    read-only opens either, while a writer holds the file) at a time;
    callers should expect this to block briefly under contention
    rather than fail outright."""
    deadline = time.monotonic() + retry_seconds
    last_err: Exception | None = None
    while True:
        try:
            return open_fn()
        except duckdb.IOException as e:
            last_err = e
            if time.monotonic() >= deadline:
                raise LedgerBusyError(str(e)) from e
            time.sleep(0.1)
        except Exception as e:  # pragma: no cover - defensive
            # Some duckdb versions raise a generic Exception for lock
            # conflicts; detect by message.
            msg = str(e).lower()
            if "lock" in msg or "conflicting" in msg or "could not set lock" in msg:
                last_err = e
                if time.monotonic() >= deadline:
                    raise LedgerBusyError(str(e)) from e
                time.sleep(0.1)
                continue
            raise
    raise LedgerBusyError(str(last_err))  # pragma: no cover


def connect(db_path: Path, retry_seconds: float = 5.0) -> duckdb.DuckDBPyConnection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    def _open():
        con = duckdb.connect(str(db_path))
        con.execute(SCHEMA_SQL)
        return con

    return _retry_on_lock(_open, retry_seconds)


def connect_read_only(
    db_path: Path, retry_seconds: float = 5.0
) -> duckdb.DuckDBPyConnection:
    """Open the ledger read-only: DuckDB itself rejects any write
    statement on this connection (InvalidInputException), so this is
    safe to hand arbitrary ad-hoc SQL to (`ev sql`) without needing to
    parse/allowlist the query ourselves. Does not create the file or
    run SCHEMA_SQL (CREATE is a write) — the ledger must already
    exist."""
    db_path = Path(db_path)
    if not db_path.exists():
        raise NotFoundError(f"no ledger at {db_path}; run `ev init` first")

    def _open():
        con = duckdb.connect(str(db_path), read_only=True)
        con.execute("SET TimeZone='UTC'")
        return con

    return _retry_on_lock(_open, retry_seconds)


def default_actor() -> str:
    env = os.environ.get("EV_ACTOR")
    if env:
        return env
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return socket.gethostname()


# --- Id resolution -----------------------------------------------------------


def resolve_event_id(con: duckdb.DuckDBPyConnection, prefix: str) -> str:
    if len(prefix) < 4:
        raise ValidationError(
            f"id prefix {prefix!r} is shorter than the minimum 4 characters"
        )
    rows = con.execute(
        "SELECT id FROM events WHERE id LIKE ? ORDER BY id", [prefix + "%"]
    ).fetchall()
    exact = [r[0] for r in rows if r[0] == prefix]
    if exact:
        return exact[0]
    ids = [r[0] for r in rows]
    if len(ids) == 0:
        raise NotFoundError(f"no event matches id prefix {prefix!r}")
    if len(ids) > 1:
        raise AmbiguousIdError(
            f"id prefix {prefix!r} is ambiguous: matches {', '.join(i[:8] for i in ids)}"
        )
    return ids[0]


def event_exists(con: duckdb.DuckDBPyConnection, event_id: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM events WHERE id = ?", [event_id]
    ).fetchone()
    return row is not None


# --- Fetching events/evidence as model objects --------------------------------


def _row_to_event(row, edges_by_id: dict[str, list[Edge]]) -> Event:
    seq, id_, kind, content, actor, occurred_at, recorded_at = row
    return Event(
        id=id_,
        seq=seq,
        kind=kind,
        content=content,
        actor=actor,
        occurred_at=parse_any_timestamp(occurred_at),
        recorded_at=parse_any_timestamp(recorded_at),
        edges=tuple(edges_by_id.get(id_, [])),
    )


def fetch_events(
    con: duckdb.DuckDBPyConnection, ids: list[str] | None = None
) -> list[Event]:
    if ids is not None:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        ev_rows = con.execute(
            f"SELECT seq, id, kind, content, actor, occurred_at, recorded_at "
            f"FROM events WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        edge_rows = con.execute(
            f"SELECT src, dst, label FROM edges WHERE src IN ({placeholders})",
            ids,
        ).fetchall()
    else:
        ev_rows = con.execute(
            "SELECT seq, id, kind, content, actor, occurred_at, recorded_at "
            "FROM events"
        ).fetchall()
        edge_rows = con.execute("SELECT src, dst, label FROM edges").fetchall()

    edges_by_id: dict[str, list[Edge]] = {}
    for src, dst, label in edge_rows:
        edges_by_id.setdefault(src, []).append(Edge(label=label, dst=dst))

    events = [_row_to_event(r, edges_by_id) for r in ev_rows]
    events.sort(key=lambda e: e.seq)
    return events


def fetch_evidence(con: duckdb.DuckDBPyConnection) -> list[EvidenceRow]:
    rows = con.execute(
        "SELECT seq, id, event_id, blob_hash, note, actor, recorded_at FROM evidence"
    ).fetchall()
    result = []
    for seq, id_, event_id, blob_hash, note, actor, recorded_at in rows:
        result.append(
            EvidenceRow(
                id=id_,
                seq=seq,
                event_id=event_id,
                blob_hash=blob_hash,
                note=note,
                actor=actor,
                recorded_at=parse_any_timestamp(recorded_at),
            )
        )
    result.sort(key=lambda e: e.seq)
    return result


def fetch_time_marks(con: duckdb.DuckDBPyConnection) -> list[TimeMark]:
    rows = con.execute(
        "SELECT seq, id, event_id, at_time, note, actor, recorded_at FROM time_marks"
    ).fetchall()
    result = []
    for seq, id_, event_id, at_time, note, actor, recorded_at in rows:
        result.append(
            TimeMark(
                id=id_,
                seq=seq,
                event_id=event_id,
                at=parse_any_timestamp(at_time),
                note=note,
                actor=actor,
                recorded_at=parse_any_timestamp(recorded_at),
            )
        )
    result.sort(key=lambda m: m.seq)
    return result


def ancestor_closure_ids(
    con: duckdb.DuckDBPyConnection, target_ids: list[str]
) -> set[str]:
    """past = target_ids UNION all ancestors reachable from them via
    outgoing edges (dst chases)."""
    if not target_ids:
        return set()
    placeholders = ",".join("?" for _ in target_ids)
    rows = con.execute(
        f"""
        WITH RECURSIVE anc(id) AS (
            SELECT id FROM (SELECT UNNEST(?::VARCHAR[]) AS id)
            UNION
            SELECT e.dst FROM edges e JOIN anc a ON e.src = a.id
        )
        SELECT id FROM anc
        """,
        [target_ids],
    ).fetchall()
    return {r[0] for r in rows}


# --- Append ----------------------------------------------------------------


@dataclass
class AppendResult:
    id: str
    created: bool
    warnings: list[str]


def append(
    con: duckdb.DuckDBPyConnection,
    *,
    kind: str,
    content: str,
    edges: list[tuple[str, str]],
    actor: str | None = None,
    at: str | None = None,
    now: datetime | None = None,
) -> AppendResult:
    """edges: list of (label, id_prefix_or_full_id). Resolves prefixes,
    validates, computes id, and inserts in one transaction. Raises
    ValidationError (writing nothing) on any violation."""
    con.execute("BEGIN TRANSACTION")
    try:
        result = append_no_txn(
            con, kind=kind, content=content, edges=edges, actor=actor, at=at, now=now
        )
        con.execute("COMMIT")
        return result
    except Exception:
        con.execute("ROLLBACK")
        raise


def append_no_txn(
    con: duckdb.DuckDBPyConnection,
    *,
    kind: str,
    content: str,
    edges: list[tuple[str, str]],
    actor: str | None = None,
    at: str | None = None,
    now: datetime | None = None,
) -> AppendResult:
    """Same as append(), but assumes the caller manages the enclosing
    transaction (BEGIN/COMMIT/ROLLBACK). Used so `ev add --evidence`
    can be atomic across the event insert and the evidence attaches."""
    now = now or utcnow()
    actor = actor or default_actor()

    validate_kind(kind)
    validate_content(content)

    warnings: list[str] = []

    resolved_edges: list[tuple[str, str]] = []
    seen_pairs = set()
    for label, prefix in edges:
        if label not in EDGE_LABELS:
            raise ValidationError(f"unknown edge label {label!r}")
        dst = resolve_event_id(con, prefix)
        if (label, dst) in seen_pairs:
            raise ValidationError(f"duplicate edge ({label!r}, {dst[:8]!r})")
        seen_pairs.add((label, dst))
        resolved_edges.append((label, dst))

    target_ids = [dst for _label, dst in resolved_edges]

    # One event may not both fulfil and drop the same target.
    fulfils_targets = {d for l, d in resolved_edges if l == "fulfils"}
    drops_targets = {d for l, d in resolved_edges if l == "drops"}
    both = fulfils_targets & drops_targets
    if both:
        raise ValidationError(
            f"event cannot both fulfil and drop {next(iter(both))[:8]}"
        )

    # Compute the event's own causal past.
    past_ids = ancestor_closure_ids(con, target_ids)
    past_events = fetch_events(con, list(past_ids)) if past_ids else []
    past_statuses = compute_status_all(past_events, [], ViewSelector())
    past_events_by_id = {e.id: e for e in past_events}
    past_live = compute_live(past_events)

    for label, dst in resolved_edges:
        target = past_events_by_id.get(dst)
        if target is None:
            raise ValidationError(f"edge target {dst[:8]} does not exist")

        if label == "needs":
            if target.kind not in OPENING_KINDS:
                raise ValidationError(
                    f"needs target {dst[:8]} is not an opening kind"
                )
            if not past_live.get(dst, False):
                raise ValidationError(
                    f"needs target {dst[:8]} is not live in its past"
                )
            if past_statuses.get(dst) == "dropped":
                raise ValidationError(
                    f"needs target {dst[:8]} is dropped in its past"
                )
        elif label in ("fulfils", "drops"):
            if target.kind not in OPENING_KINDS:
                raise ValidationError(
                    f"{label} target {dst[:8]} is not an opening kind"
                )
            if not past_live.get(dst, False):
                raise ValidationError(
                    f"{label} target {dst[:8]} is not live in its past"
                )
            st = past_statuses.get(dst)
            if st not in ("ready", "blocked", "stuck"):
                raise ValidationError(
                    f"{label} target {dst[:8]} is already closed ({st}) in its past"
                )
            if st == "blocked":
                warnings.append(
                    f"closing {dst[:8]} while it is blocked"
                )
        elif label == "retracts":
            if not past_live.get(dst, False):
                raise ValidationError(
                    f"retracts target {dst[:8]} is not live in its past"
                )
        elif label == "from":
            pass

    # Timestamps.
    if at is not None:
        occurred_at = parse_strict_iso8601(at)
        if occurred_at > now:
            raise ValidationError("--at must not be in the future")
    else:
        occurred_at = None  # resolved to recorded_at below

    parent_recorded_ats = [
        past_events_by_id[dst].recorded_at for dst in target_ids if dst in past_events_by_id
    ]
    recorded_at = max([now] + parent_recorded_ats)

    if occurred_at is None:
        occurred_at = recorded_at
    if occurred_at > recorded_at:
        raise ValidationError("occurred_at must be <= recorded_at")

    new_id = compute_event_id(
        actor=actor,
        content=content,
        edges=resolved_edges,
        kind=kind,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
    )

    if event_exists(con, new_id):
        return AppendResult(id=new_id, created=False, warnings=[])

    con.execute(
        "INSERT INTO events (id, kind, content, actor, occurred_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [new_id, kind, content, actor, occurred_at, recorded_at],
    )
    for label, dst in resolved_edges:
        con.execute(
            "INSERT INTO edges (src, dst, label) VALUES (?, ?, ?)",
            [new_id, dst, label],
        )

    return AppendResult(id=new_id, created=True, warnings=warnings)


# --- Time marks --------------------------------------------------------------


@dataclass
class MarkResult:
    id: str
    created: bool


def mark_time(
    con: duckdb.DuckDBPyConnection,
    *,
    event_id: str,
    at: str,
    note: str = "",
    actor: str | None = None,
    now: datetime | None = None,
) -> MarkResult:
    con.execute("BEGIN TRANSACTION")
    try:
        result = mark_time_no_txn(
            con, event_id=event_id, at=at, note=note, actor=actor, now=now
        )
        con.execute("COMMIT")
        return result
    except Exception:
        con.execute("ROLLBACK")
        raise


def mark_time_no_txn(
    con: duckdb.DuckDBPyConnection,
    *,
    event_id: str,
    at: str,
    note: str = "",
    actor: str | None = None,
    now: datetime | None = None,
) -> MarkResult:
    """Set/correct the single instant an event is or was scheduled for.
    `at` is parsed strictly (explicit offset/Z required) but, unlike
    `--at` on `ev add`, is NOT constrained to the past — scheduling a
    future instant is the whole point. Idempotent: an identical
    (actor, at, event_id, note, recorded_at) writes nothing and
    returns the existing id. `event_id` may be any existing event,
    retracted or not (same rule as evidence)."""
    now = now or utcnow()
    actor = actor or default_actor()

    if not event_exists(con, event_id):
        raise ValidationError(f"event {event_id[:8]} does not exist")

    at_dt = parse_strict_iso8601(at)

    event_recorded_at = con.execute(
        "SELECT recorded_at FROM events WHERE id = ?", [event_id]
    ).fetchone()[0]
    event_recorded_at = parse_any_timestamp(event_recorded_at)
    recorded_at = max(now, event_recorded_at)

    mark_id = compute_time_mark_id(
        actor=actor, at=at_dt, event_id=event_id, note=note, recorded_at=recorded_at
    )

    existing = con.execute(
        "SELECT id FROM time_marks WHERE id = ?", [mark_id]
    ).fetchone()
    if existing is not None:
        return MarkResult(id=mark_id, created=False)

    con.execute(
        "INSERT INTO time_marks (id, event_id, at_time, note, actor, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [mark_id, event_id, at_dt, note, actor, recorded_at],
    )
    return MarkResult(id=mark_id, created=True)
