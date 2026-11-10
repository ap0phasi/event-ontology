"""Core types, canonical hashing, and validation for ev.

This module has no I/O. It defines the shape of events and evidence,
how their ids are computed (content hashing), and the structural
validation rules that do not require looking at the rest of the
ledger (format checks). Validation rules that require the ledger's
causal past live in store.py / reference.py.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- Constants -------------------------------------------------------------

# The three kinds that carry open/closed semantics. This is the single
# place this set is defined.
OPENING_KINDS = frozenset({"notice", "ask", "intend"})

# The closed set of edge labels. Direction is always new event -> existing
# event (src is the new event, dst is the existing target).
EDGE_LABELS = frozenset({"from", "needs", "fulfils", "drops", "retracts"})

KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ValidationError(ValueError):
    """Raised for any append/attach rule violation. Caller should treat
    this as 'write nothing, exit 1'."""


# --- Timestamp helpers -------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(dt: datetime) -> str:
    """Format a timestamp the canonical way: UTC, microseconds, 'Z' suffix."""
    if dt.tzinfo is None:
        raise ValueError("naive timestamp cannot be formatted canonically")
    dt = dt.astimezone(timezone.utc)
    return dt.strftime(TIMESTAMP_FORMAT)


def parse_strict_iso8601(text: str) -> datetime:
    """Parse an ISO 8601 timestamp that MUST carry an explicit offset or
    'Z'. Naive timestamps are rejected. Returns a UTC-aware datetime."""
    s = text.strip()
    if s.endswith("Z") or s.endswith("z"):
        core = s[:-1]
        try:
            dt = datetime.fromisoformat(core)
        except ValueError as e:
            raise ValidationError(f"invalid ISO 8601 timestamp: {text!r}") from e
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as e:
            raise ValidationError(f"invalid ISO 8601 timestamp: {text!r}") from e
        if dt.tzinfo is None:
            raise ValidationError(
                f"timestamp {text!r} has no explicit UTC offset or 'Z'"
            )
    return dt.astimezone(timezone.utc)


def parse_any_timestamp(text: str) -> datetime:
    """Parse a timestamp for display/storage purposes where naive is
    assumed UTC (used when reading back from DuckDB, which stores naive
    TIMESTAMP as UTC by convention)."""
    if isinstance(text, datetime):
        dt = text
    else:
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --- Canonical JSON / hashing -------------------------------------------------


def _canonical_bytes(obj) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_event_json(
    *,
    actor: str,
    content: str,
    edges: list[tuple[str, str]],
    kind: str,
    occurred_at: datetime,
    recorded_at: datetime,
) -> bytes:
    sorted_edges = sorted([[label, dst] for label, dst in edges])
    obj = {
        "actor": actor,
        "content": content,
        "edges": sorted_edges,
        "kind": kind,
        "occurred_at": format_timestamp(occurred_at),
        "recorded_at": format_timestamp(recorded_at),
    }
    return _canonical_bytes(obj)


def compute_event_id(
    *,
    actor: str,
    content: str,
    edges: list[tuple[str, str]],
    kind: str,
    occurred_at: datetime,
    recorded_at: datetime,
) -> str:
    data = canonical_event_json(
        actor=actor,
        content=content,
        edges=edges,
        kind=kind,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
    )
    return hashlib.sha256(data).hexdigest()


def canonical_evidence_json(
    *,
    actor: str,
    blob_hash: str,
    event_id: str,
    note: str,
    recorded_at: datetime,
) -> bytes:
    obj = {
        "actor": actor,
        "blob_hash": blob_hash,
        "event_id": event_id,
        "note": note,
        "recorded_at": format_timestamp(recorded_at),
    }
    return _canonical_bytes(obj)


def compute_evidence_id(
    *,
    actor: str,
    blob_hash: str,
    event_id: str,
    note: str,
    recorded_at: datetime,
) -> str:
    data = canonical_evidence_json(
        actor=actor,
        blob_hash=blob_hash,
        event_id=event_id,
        note=note,
        recorded_at=recorded_at,
    )
    return hashlib.sha256(data).hexdigest()


def canonical_time_mark_json(
    *,
    actor: str,
    at: datetime,
    event_id: str,
    note: str,
    recorded_at: datetime,
) -> bytes:
    obj = {
        "actor": actor,
        "at": format_timestamp(at),
        "event_id": event_id,
        "note": note,
        "recorded_at": format_timestamp(recorded_at),
    }
    return _canonical_bytes(obj)


def compute_time_mark_id(
    *,
    actor: str,
    at: datetime,
    event_id: str,
    note: str,
    recorded_at: datetime,
) -> str:
    data = canonical_time_mark_json(
        actor=actor, at=at, event_id=event_id, note=note, recorded_at=recorded_at
    )
    return hashlib.sha256(data).hexdigest()


# --- Structural validation (no ledger access required) -------------------------


def validate_kind(kind: str) -> None:
    if not KIND_PATTERN.match(kind):
        raise ValidationError(
            f"kind {kind!r} does not match ^[a-z][a-z0-9_-]{{0,31}}$"
        )


def validate_content(content: str) -> None:
    if not content:
        raise ValidationError("content must be non-empty")


def validate_edges_structural(
    edges: list[tuple[str, str]], src_placeholder: str | None = None
) -> None:
    """Checks that don't require the DB: labels in closed set, no
    duplicate (label, dst), no self-edges (dst == src_placeholder, used
    only when src id is already known, which it normally is not before
    hashing -- self-edges are impossible by construction since ids are
    content hashes of events that don't yet exist, but we still check
    against any explicitly-given 'self' marker if provided)."""
    seen = set()
    for label, dst in edges:
        if label not in EDGE_LABELS:
            raise ValidationError(f"unknown edge label {label!r}")
        if (label, dst) in seen:
            raise ValidationError(f"duplicate edge ({label!r}, {dst!r})")
        seen.add((label, dst))
        if src_placeholder is not None and dst == src_placeholder:
            raise ValidationError("self-edges are not allowed")


# --- Dataclasses for in-memory representation ---------------------------------


@dataclass(frozen=True)
class Edge:
    label: str
    dst: str


@dataclass(frozen=True)
class Event:
    id: str
    seq: int
    kind: str
    content: str
    actor: str
    occurred_at: datetime
    recorded_at: datetime
    edges: tuple[Edge, ...] = field(default_factory=tuple)

    @property
    def is_opening(self) -> bool:
        return self.kind in OPENING_KINDS


@dataclass(frozen=True)
class EvidenceRow:
    id: str
    seq: int
    event_id: str
    blob_hash: str
    note: str
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class TimeMark:
    """A single, correctable instant attached to an event. An event is
    always an instant itself (`occurred_at`, immutable, constrained to
    the past); a TimeMark is how you say *when something not-yet-done
    is supposed to happen* (unconstrained — future is the point), or
    correct a mistaken instant later, without touching the event's
    identity. For a given event_id, the most-recently-submitted
    TimeMark (by recorded_at, then seq) is the current one — same
    latest-wins pattern as `blob_locations`. There is no label/range:
    one event, at most one current "when"."""

    id: str
    seq: int
    event_id: str
    at: datetime
    note: str
    actor: str
    recorded_at: datetime


@dataclass(frozen=True)
class BlobRow:
    hash: str
    media_type: str
    size: int
    recorded_at: datetime


@dataclass(frozen=True)
class BlobLocationRow:
    hash: str
    uri: str
    recorded_at: datetime


ID_PREFIX_MIN = 4
ID_DISPLAY_LEN = 8
