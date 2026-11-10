"""View selection and derivation against the DuckDB-backed store.

Ancestor/line computation uses a recursive SQL CTE (see
store.ancestor_closure_ids). Status derivation reuses the pure
reference-model functions over the events/evidence fetched for the
view, which is differentially tested against reference.py operating on
hand-built in-memory ledgers (property P5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb

from .model import EvidenceRow, Event, TimeMark, parse_strict_iso8601
from .reference import (
    ViewSelector,
    ancestors as ref_ancestors,
    compute_closers,
    compute_live,
    compute_status_all,
    current_time_marks as ref_current_time_marks,
    diff_lines as ref_diff_lines,
    effective_time as ref_effective_time,
    is_overdue as ref_is_overdue,
    tips as ref_tips,
    visible_time_marks as ref_visible_time_marks,
)
from .store import (
    ancestor_closure_ids,
    fetch_events,
    fetch_evidence,
    fetch_time_marks,
    resolve_event_id,
)


@dataclass
class ViewArgs:
    as_of: str | None = None
    line: str | None = None


def build_selector(
    con: duckdb.DuckDBPyConnection, args: ViewArgs
) -> ViewSelector:
    as_of_seq = None
    as_of_time = None
    if args.as_of is not None:
        s = args.as_of
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            as_of_seq = int(s)
        else:
            as_of_time = parse_strict_iso8601(s)

    line_id = None
    if args.line is not None:
        line_id = resolve_event_id(con, args.line)

    return ViewSelector(as_of_seq=as_of_seq, as_of_time=as_of_time, line=line_id)


def load_ledger(
    con: duckdb.DuckDBPyConnection,
) -> tuple[list[Event], list[EvidenceRow]]:
    return fetch_events(con), fetch_evidence(con)


def view_events_and_evidence(
    con: duckdb.DuckDBPyConnection, selector: ViewSelector
) -> tuple[list[Event], list[EvidenceRow]]:
    from .reference import apply_view

    events, evidence = load_ledger(con)
    return apply_view(events, evidence, selector)


def statuses_in_view(
    con: duckdb.DuckDBPyConnection, selector: ViewSelector
) -> dict[str, str]:
    events, evidence = load_ledger(con)
    return compute_status_all(events, evidence, selector)


def status_of_id(
    con: duckdb.DuckDBPyConnection, event_id: str, selector: ViewSelector
) -> str | None:
    return statuses_in_view(con, selector).get(event_id)


def list_tips(con: duckdb.DuckDBPyConnection, selector: ViewSelector) -> list[str]:
    events, evidence = load_ledger(con)
    return ref_tips(events, evidence, selector)


def diff(con: duckdb.DuckDBPyConnection, a_prefix: str, b_prefix: str):
    a = resolve_event_id(con, a_prefix)
    b = resolve_event_id(con, b_prefix)
    events, _ = load_ledger(con)
    return ref_diff_lines(events, a, b)


def ancestors_of(con: duckdb.DuckDBPyConnection, event_id: str) -> set[str]:
    return ancestor_closure_ids(con, [event_id]) - {event_id}


def descendants_of(con: duckdb.DuckDBPyConnection, event_id: str) -> set[str]:
    """All events that (transitively) have an edge pointing at event_id."""
    rows = con.execute(
        """
        WITH RECURSIVE descendants(id) AS (
            SELECT src FROM edges WHERE dst = ?
            UNION
            SELECT e.src FROM edges e JOIN descendants d ON e.dst = d.id
        )
        SELECT id FROM descendants
        """,
        [event_id],
    ).fetchall()
    return {r[0] for r in rows}


def current_time_marks_in_view(
    con: duckdb.DuckDBPyConnection, selector: ViewSelector
) -> dict[str, TimeMark]:
    """event_id -> its current (latest-submitted, visible-in-view)
    TimeMark, if any. Rewinding a view rewinds which correction was
    "current" at that point, same as everything else here."""
    from .reference import apply_view

    events, evidence = load_ledger(con)
    time_marks = fetch_time_marks(con)
    view_events, _ = apply_view(events, evidence, selector)
    view_event_ids = {e.id for e in view_events}
    visible = ref_visible_time_marks(time_marks, view_event_ids, selector)
    return ref_current_time_marks(visible)


def overdue_in_view(
    con: duckdb.DuckDBPyConnection, selector: ViewSelector, now: datetime
) -> set[str]:
    """ids of open events whose current TimeMark has passed `now`."""
    statuses = statuses_in_view(con, selector)
    marks = current_time_marks_in_view(con, selector)
    return {
        eid
        for eid, st in statuses.items()
        if ref_is_overdue(st, marks.get(eid), now)
    }
