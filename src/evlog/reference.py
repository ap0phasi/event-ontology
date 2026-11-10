"""Pure-Python reference implementation of sections 3-5 of the spec.

This operates purely on in-memory sequences of `Event` and `EvidenceRow`
objects (see model.py). It has no I/O and is the executable spec: it is
used directly by Hypothesis property tests, and `views.py` is
differentially tested against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .model import Edge, Event, EvidenceRow, OPENING_KINDS, TimeMark

# Status strings
STATUS_CONTESTED = "contested"
STATUS_FULFILLED = "fulfilled"
STATUS_DROPPED = "dropped"
STATUS_STUCK = "stuck"
STATUS_BLOCKED = "blocked"
STATUS_READY = "ready"
STATUS_INERT = "-"
STATUS_RETRACTED = "retracted"

OPEN_STATUSES = {STATUS_READY, STATUS_BLOCKED, STATUS_STUCK}
ALL_STATUSES = {
    STATUS_READY,
    STATUS_BLOCKED,
    STATUS_STUCK,
    STATUS_FULFILLED,
    STATUS_DROPPED,
    STATUS_CONTESTED,
    "open",
}


@dataclass(frozen=True)
class ViewSelector:
    """A down-closed selection of the ledger.

    as_of_seq: events/evidence with seq <= as_of_seq
    as_of_time: events/evidence with recorded_at <= as_of_time
    line: id of a head; selection is ancestors(line) + {line}
    Selectors combine by intersection. None of them set = whole ledger.
    """

    as_of_seq: int | None = None
    as_of_time: datetime | None = None
    line: str | None = None


# --- View application --------------------------------------------------------


def ancestors(event_id: str, events_by_id: dict[str, Event]) -> set[str]:
    """All ancestors of event_id (not including itself), following every
    edge regardless of label."""
    seen: set[str] = set()
    stack = [event_id]
    while stack:
        cur = stack.pop()
        ev = events_by_id.get(cur)
        if ev is None:
            continue
        for edge in ev.edges:
            if edge.dst not in seen:
                seen.add(edge.dst)
                stack.append(edge.dst)
    return seen


def apply_view(
    events: list[Event],
    evidence: list[EvidenceRow],
    selector: ViewSelector,
) -> tuple[list[Event], list[EvidenceRow]]:
    """Return the (events, evidence) visible under the given selector,
    both down-closed and sorted by seq ascending."""
    events_by_id = {e.id: e for e in events}

    allowed_ids: set[str] | None = None
    if selector.line is not None:
        if selector.line not in events_by_id:
            allowed_ids = set()
        else:
            allowed_ids = ancestors(selector.line, events_by_id) | {
                selector.line
            }

    def event_ok(e: Event) -> bool:
        if allowed_ids is not None and e.id not in allowed_ids:
            return False
        if selector.as_of_seq is not None and e.seq > selector.as_of_seq:
            return False
        if (
            selector.as_of_time is not None
            and e.recorded_at > selector.as_of_time
        ):
            return False
        return True

    view_events = [e for e in events if event_ok(e)]
    view_event_ids = {e.id for e in view_events}

    def evidence_ok(ev: EvidenceRow) -> bool:
        if ev.event_id not in view_event_ids:
            return False
        if selector.as_of_seq is not None and ev.seq > selector.as_of_seq:
            return False
        if (
            selector.as_of_time is not None
            and ev.recorded_at > selector.as_of_time
        ):
            return False
        return True

    view_evidence = [ev for ev in evidence if evidence_ok(ev)]

    view_events.sort(key=lambda e: e.seq)
    view_evidence.sort(key=lambda e: e.seq)
    return view_events, view_evidence


def visible_time_marks(
    time_marks: list[TimeMark],
    view_event_ids: set[str],
    selector: ViewSelector,
) -> list[TimeMark]:
    """Same visibility rule as evidence: a TimeMark is shown when its
    event is in the view and the row itself is inside any --as-of
    bound. Unlike evidence, there is no `--line` restriction on the
    mark's own event — it's already implied by view_event_ids."""

    def ok(m) -> bool:
        if m.event_id not in view_event_ids:
            return False
        if selector.as_of_seq is not None and m.seq > selector.as_of_seq:
            return False
        if (
            selector.as_of_time is not None
            and m.recorded_at > selector.as_of_time
        ):
            return False
        return True

    result = [m for m in time_marks if ok(m)]
    result.sort(key=lambda m: m.seq)
    return result


def current_time_marks(time_marks: list[TimeMark]) -> dict[str, TimeMark]:
    """For each event_id, the most-recently-submitted TimeMark (by
    recorded_at, then seq to break ties) — the latest-wins pattern
    also used by `blob_locations`. Correcting a mistaken time is just
    appending a new row; nothing is edited. Callers should pass
    already-view-filtered marks (see `visible_time_marks`) so that
    rewinding a view also rewinds which correction was "current" at
    that point."""
    current: dict[str, TimeMark] = {}
    for m in time_marks:
        existing = current.get(m.event_id)
        if existing is None or (m.recorded_at, m.seq) >= (
            existing.recorded_at,
            existing.seq,
        ):
            current[m.event_id] = m
    return current


def effective_time(event: Event, current_mark: TimeMark | None) -> datetime:
    """The time to sort/display an event by: its current TimeMark if
    one has ever been set (covers future-scheduled and corrected
    instants), else its own `occurred_at` (covers the common case of
    an already-happened event logged accurately, which never needs a
    TimeMark at all)."""
    return current_mark.at if current_mark is not None else event.occurred_at


def is_overdue(status: str, current_mark: TimeMark | None, now: datetime) -> bool:
    """An open event becomes overdue only if it was explicitly given a
    time to happen by and that time has passed. No TimeMark means no
    deadline — we deliberately do not treat a bare `occurred_at` (when
    the intention/notice/ask was *written*) as an implied deadline."""
    if status not in OPEN_STATUSES:
        return False
    if current_mark is None:
        return False
    return current_mark.at < now


# --- Core derivations ----------------------------------------------------------


def compute_live(view_events: list[Event]) -> dict[str, bool]:
    """live(E, V) for every event E in view_events (already sorted by
    seq, but we sort defensively). Iterate in descending seq so that
    retractors are resolved before their targets are evaluated."""
    by_seq_desc = sorted(view_events, key=lambda e: -e.seq)
    ids_in_view = {e.id for e in view_events}
    live: dict[str, bool] = {}

    # retracts_of[target_id] = list of (retractor_id) with edge retracts->target
    retracts_of: dict[str, list[str]] = {}
    for e in view_events:
        for edge in e.edges:
            if edge.label == "retracts":
                retracts_of.setdefault(edge.dst, []).append(e.id)

    for e in by_seq_desc:
        retractors = retracts_of.get(e.id, [])
        is_retracted_by_live = any(
            r in ids_in_view and live.get(r, False) for r in retractors
        )
        live[e.id] = not is_retracted_by_live
    return live


def compute_closers(
    view_events: list[Event], live: dict[str, bool]
) -> dict[str, list[tuple[str, str]]]:
    """closers(E, V): list of (closer_id, label) for label in
    {fulfils, drops}, for live closer events."""
    closers: dict[str, list[tuple[str, str]]] = {}
    for e in view_events:
        if not live.get(e.id, False):
            continue
        for edge in e.edges:
            if edge.label in ("fulfils", "drops"):
                closers.setdefault(edge.dst, []).append((e.id, edge.label))
    return closers


def compute_status_all(
    events: list[Event],
    evidence: list[EvidenceRow],
    selector: ViewSelector | None = None,
) -> dict[str, str]:
    """Compute status(E, V) for every event visible in the view. Events
    not in the view are absent from the result dict."""
    if selector is None:
        selector = ViewSelector()
    view_events, _view_evidence = apply_view(events, evidence, selector)
    view_ids = {e.id for e in view_events}
    live = compute_live(view_events)
    closers = compute_closers(view_events, live)

    statuses: dict[str, str] = {}
    events_by_id = {e.id: e for e in view_events}

    for e in view_events:
        if not live.get(e.id, False):
            statuses[e.id] = STATUS_RETRACTED
            continue
        if not e.is_opening:
            statuses[e.id] = STATUS_INERT
            continue
        my_closers = closers.get(e.id, [])
        if len(my_closers) >= 2:
            statuses[e.id] = STATUS_CONTESTED
            continue
        if len(my_closers) == 1:
            _cid, label = my_closers[0]
            statuses[e.id] = (
                STATUS_FULFILLED if label == "fulfils" else STATUS_DROPPED
            )
            continue
        # No closer: check needs edges.
        needs_targets = [
            edge.dst for edge in e.edges if edge.label == "needs"
        ]
        if needs_targets:
            # Rule: stuck wins over blocked if ANY needs target is
            # dropped-or-not-live; else blocked if any is not fulfilled.
            stuck = False
            blocked = False
            for t in needs_targets:
                if t not in view_ids or not live.get(t, False):
                    stuck = True
                    continue
                t_status = statuses.get(t)
                if t_status is None:
                    # Shouldn't happen: t has lower seq, already computed
                    # because we iterate view_events in seq order.
                    t_status = STATUS_READY
                if t_status == STATUS_DROPPED:
                    stuck = True
                elif t_status != STATUS_FULFILLED:
                    blocked = True
            if stuck:
                statuses[e.id] = STATUS_STUCK
            elif blocked:
                statuses[e.id] = STATUS_BLOCKED
            else:
                statuses[e.id] = STATUS_READY
            continue
        statuses[e.id] = STATUS_READY

    _ = events_by_id
    return statuses


def status_of(
    event_id: str,
    events: list[Event],
    evidence: list[EvidenceRow],
    selector: ViewSelector | None = None,
) -> str | None:
    statuses = compute_status_all(events, evidence, selector)
    return statuses.get(event_id)


def tips(events: list[Event], evidence: list[EvidenceRow], selector=None):
    """Live events in the view with no live children (any label)."""
    if selector is None:
        selector = ViewSelector()
    view_events, _ = apply_view(events, evidence, selector)
    live = compute_live(view_events)
    has_child: set[str] = set()
    for e in view_events:
        if not live.get(e.id, False):
            continue
        for edge in e.edges:
            has_child.add(edge.dst)
    result = []
    for e in view_events:
        if live.get(e.id, False) and e.id not in has_child:
            result.append(e.id)
    return result


def diff_lines(events: list[Event], a: str, b: str):
    """Events only in line(a), and only in line(b)."""
    events_by_id = {e.id: e for e in events}
    anc_a = ancestors(a, events_by_id) | {a} if a in events_by_id else set()
    anc_b = ancestors(b, events_by_id) | {b} if b in events_by_id else set()
    return anc_a - anc_b, anc_b - anc_a
