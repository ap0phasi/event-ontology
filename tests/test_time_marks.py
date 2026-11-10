"""Tests for time_marks: the append-only, latest-wins "when" channel
that sits beside events (never an attribute on them), used for
scheduling future work and correcting dates without rewriting history.

Includes realistic project/life-management scenarios per the actual
feedback that prompted this feature: events are instants, dependencies
and deadlines matter, and corrections must not rewrite the past.
"""

from __future__ import annotations

import json

import pytest

from helpers import run_cli


# --- Core mechanics -----------------------------------------------------------


def test_when_sets_and_show_reports_it(ledger):
    i = ledger.add("intend", "renew passport")
    ledger.when(i, "2030-01-01T00:00:00Z", note="deadline")
    obj = ledger.show(i)
    assert obj["when"] == "2030-01-01T00:00:00.000000Z"
    assert obj["overdue"] is False


def test_future_time_is_allowed_unlike_event_at(ledger):
    """The whole point: --at on `ev add` rejects the future (it's
    asserting something already occurred), but `ev when` must not,
    since scheduling ahead is the primary use case."""
    i = ledger.add("intend", "future task")
    # should not raise / exit nonzero
    mark_id = ledger.when(i, "2099-06-15T12:00:00Z")
    assert mark_id

    code, out, err = ledger.run(
        ["add", "do", "x", "--at", "2099-06-15T12:00:00Z"]
    )
    assert code == 1  # occurred_at future is still rejected, unchanged


def test_correcting_a_time_is_latest_wins_not_mutation(ledger):
    i = ledger.add("intend", "flight")
    ledger.when(i, "2030-01-01T08:00:00Z", note="original booking")
    code, out, err = ledger.run(
        ["sql", "--json", f"SELECT seq FROM time_marks WHERE event_id = '{i}'"]
    )
    assert code == 0, err
    first_mark_seq = json.loads(out.strip().splitlines()[-1])["seq"]

    ledger.when(i, "2030-01-02T09:30:00Z", note="rescheduled by airline")
    obj = ledger.show(i)
    assert obj["when"] == "2030-01-02T09:30:00.000000Z"

    # the old value is still visible from a view before the correction
    obj_before = ledger.show(i, as_of=first_mark_seq)
    assert obj_before["when"] == "2030-01-01T08:00:00.000000Z"


def test_no_time_mark_falls_back_to_occurred_at_for_display(ledger):
    i = ledger.add("intend", "already handled, logged accurately")
    obj = ledger.show(i)
    assert obj["when"] is None  # no mark set
    sched = ledger.schedule()
    row = next(r for r in sched if r["id"] == i)
    # effective_time fell back to occurred_at
    assert row["when"] == obj["occurred_at"]
    assert row["when_is_mark"] is False


def test_time_mark_does_not_affect_status(ledger):
    """Evidence is inert; so is a time mark. Tagging a date must never
    change any derived status."""
    q = ledger.add("ask", "which vendor?")
    before = ledger.status(q)
    ledger.when(q, "2030-01-01T00:00:00Z")
    after = ledger.status(q)
    assert before == after == "ready"


def test_time_mark_allowed_on_retracted_event(ledger):
    n = ledger.add("notice", "n")
    ledger.add("retract", "oops", retracts=[n])
    # should not raise, same rule as evidence
    mark_id = ledger.when(n, "2030-01-01T00:00:00Z")
    assert mark_id


def test_when_requires_existing_event(ledger):
    code, out, err = ledger.run(["when", "deadbeef", "2030-01-01T00:00:00Z"])
    assert code == 1


def test_when_rejects_naive_timestamp(ledger):
    i = ledger.add("intend", "x")
    code, out, err = ledger.run(["when", i, "2030-01-01T00:00:00"])
    assert code == 1


# --- Overdue derivation ----------------------------------------------------------


def test_overdue_requires_open_status_and_past_mark(ledger):
    i = ledger.add("intend", "do the thing")
    ledger.when(i, "2020-01-01T00:00:00Z")  # far past
    assert ledger.ids("ls", "--overdue") == {i}

    # closing it removes it from overdue (status no longer open)
    ledger.do(i, "done")
    assert ledger.ids("ls", "--overdue") == set()


def test_overdue_false_without_a_mark_even_if_old(ledger):
    """An open item with no time mark is never overdue -- occurred_at
    (when the intention was written) is deliberately not treated as an
    implied deadline."""
    i = ledger.add("intend", "someday", at="2020-01-01T00:00:00Z")
    assert ledger.status(i) == "ready"
    assert ledger.ids("ls", "--overdue") == set()


def test_overdue_false_for_future_mark(ledger):
    i = ledger.add("intend", "future task")
    ledger.when(i, "2099-01-01T00:00:00Z")
    assert ledger.ids("ls", "--overdue") == set()


# --- ev schedule -----------------------------------------------------------------


def test_schedule_sorts_by_effective_time_and_filters(ledger):
    a = ledger.add("intend", "first")
    ledger.when(a, "2030-03-01T00:00:00Z")
    b = ledger.add("intend", "second")
    ledger.when(b, "2030-01-01T00:00:00Z")
    c = ledger.add("intend", "third")
    ledger.when(c, "2030-02-01T00:00:00Z")

    sched = ledger.schedule()
    ids_in_order = [r["id"] for r in sched if r["id"] in (a, b, c)]
    assert ids_in_order == [b, c, a]

    sched_range = ledger.schedule(
        "--from-time", "2030-01-15T00:00:00Z", "--to-time", "2030-02-15T00:00:00Z"
    )
    assert [r["id"] for r in sched_range] == [c]


def test_schedule_kind_and_status_filters(ledger):
    i = ledger.add("intend", "i1")
    ledger.when(i, "2030-01-01T00:00:00Z")
    n = ledger.add("notice", "n1")
    ledger.when(n, "2030-01-02T00:00:00Z")

    sched = ledger.schedule("--kind", "notice")
    assert [r["id"] for r in sched] == [n]

    ledger.do(i, "done")
    sched = ledger.schedule("--status", "fulfilled")
    assert [r["id"] for r in sched] == [i]


# --- Append-only / idempotency / sequencing ---------------------------------------


def test_time_marks_share_the_append_only_sequence(ledger):
    i = ledger.add("intend", "x")
    j = ledger.add("intend", "y")
    ledger.when(i, "2030-01-01T00:00:00Z")
    ledger.when(j, "2030-01-02T00:00:00Z")
    code, out, err = ledger.run(["verify"])
    assert code == 0, out + err


def test_re_tagging_identical_content_is_idempotent(tmp_path):
    """At the storage layer (where `now` can be frozen), identical
    (actor, at, event_id, note, recorded_at) must be a true no-op --
    same pattern as evidence's P10."""
    from evlog import store as store_mod
    from datetime import datetime, timezone

    con = store_mod.connect(tmp_path / ".ledger" / "ledger.duckdb")
    try:
        i = store_mod.append(con, kind="intend", content="x", edges=[]).id
        frozen_now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        r1 = store_mod.mark_time(
            con, event_id=i, at="2030-01-01T00:00:00Z", now=frozen_now, actor="a"
        )
        before = con.execute("SELECT count(*) FROM time_marks").fetchone()[0]
        r2 = store_mod.mark_time(
            con, event_id=i, at="2030-01-01T00:00:00Z", now=frozen_now, actor="a"
        )
        after = con.execute("SELECT count(*) FROM time_marks").fetchone()[0]
        assert r1.id == r2.id
        assert before == after
    finally:
        con.close()


def test_export_import_round_trip_preserves_time_marks(ledger, tmp_path):
    i = ledger.add("intend", "x")
    ledger.when(i, "2030-01-01T00:00:00Z", note="original")

    export_path = tmp_path / "export.jsonl"
    code, out, err = ledger.run(["export", str(export_path)])
    assert code == 0

    other = tmp_path / "other"
    other.mkdir()
    code, out, err = run_cli(["init"], other)
    assert code == 0
    code, out, err = run_cli(["import", str(export_path)], other)
    assert code == 0
    assert "time_marks +1" in out

    code, out, err = run_cli(["show", "--json", i], other)
    assert code == 0
    obj = json.loads(out.strip().splitlines()[-1])
    assert obj["when"] == "2030-01-01T00:00:00.000000Z"

    code, out, err = run_cli(["verify"], other)
    assert code == 0


# --- Realistic scenario: a trip with dependencies and a schedule ------------------


def test_scenario_galapagos_trip_dependencies_and_schedule(ledger):
    """Events are instants; a 'trip' is modeled as several of them
    (depart, return) hanging off a parent intend, each independently
    time-tagged, with a real --needs dependency chain and a deadline
    that can go overdue."""
    trip = ledger.add("intend", "go to the Galapagos")
    passport = ledger.add("intend", "renew passport", **{"from": [trip]})
    ledger.when(passport, "2030-01-01T00:00:00Z", note="renewal deadline")

    book = ledger.add(
        "intend", "book flights", **{"from": [trip], "needs": [passport]}
    )
    assert ledger.status(book) == "blocked"
    assert book not in ledger.ids("ls", "--status", "ready")

    ledger.do(passport, "passport renewed")
    assert ledger.status(book) == "ready"
    ledger.do(book, "booked LATAM flights")

    depart = ledger.add("intend", "depart for Galapagos", **{"from": [trip]})
    ledger.when(depart, "2030-02-01T08:15:00-06:00", note="LATAM 2546")

    arrive_home = ledger.add(
        "intend", "return from Galapagos", **{"from": [trip], "needs": [depart]}
    )
    ledger.when(arrive_home, "2030-02-08T19:40:00-06:00", note="LATAM 2547")

    # can't have "returned" before "departed" is fulfilled
    assert ledger.status(arrive_home) == "blocked"

    sched = ledger.schedule()
    sched_ids_in_order = [
        r["id"] for r in sched if r["id"] in (passport, depart, arrive_home)
    ]
    assert sched_ids_in_order == [passport, depart, arrive_home]

    # the airline reschedules the return flight -- a correction, not a
    # new event, and it doesn't disturb the dependency graph
    ledger.when(arrive_home, "2030-02-09T06:00:00-06:00", note="delayed by airline")
    obj = ledger.show(arrive_home)
    assert obj["when"] == "2030-02-09T12:00:00.000000Z"  # UTC
    assert ledger.status(arrive_home) == "blocked"  # unaffected by the date change

    ledger.do(depart, "departed on time")
    assert ledger.status(arrive_home) == "ready"

    assert ledger.ids("ls", "--overdue") == set()
