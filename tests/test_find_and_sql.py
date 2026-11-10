"""Tests for `ev find` (keyword search) and `ev sql` (read-only ad-hoc
SQL escape hatch), added so an agent doesn't need to feel boxed in by
the documented subcommands when exploring a ledger."""

from __future__ import annotations

import json

import pytest

from helpers import run_cli


def test_find_matches_content_case_insensitively(ledger):
    a = ledger.add("intend", "Review Sarah's PR")
    b = ledger.add("notice", "unrelated thing")
    code, out, err = ledger.run(["find", "sarah", "--json"])
    assert code == 0
    ids = {json.loads(l)["id"] for l in out.splitlines() if l.strip()}
    assert ids == {a}


def test_find_respects_kind_and_status_and_retracted_filters(ledger):
    n = ledger.add("notice", "flaky ci pipeline")
    d = ledger.add("do", "fixed flaky ci pipeline", fulfils=[n])

    # status filter
    code, out, err = ledger.run(["find", "flaky", "--status", "fulfilled", "--json"])
    assert code == 0
    ids = {json.loads(l)["id"] for l in out.splitlines() if l.strip()}
    assert ids == {n}

    # kind filter
    code, out, err = ledger.run(["find", "flaky", "--kind", "do", "--json"])
    ids = {json.loads(l)["id"] for l in out.splitlines() if l.strip()}
    assert ids == {d}

    # retracted events hidden unless asked for
    r = ledger.add("retract", "oops", retracts=[d])
    code, out, err = ledger.run(["find", "flaky", "--kind", "do", "--json"])
    ids = {json.loads(l)["id"] for l in out.splitlines() if l.strip()}
    assert ids == set()
    code, out, err = ledger.run(
        ["find", "flaky", "--kind", "do", "--include-retracted", "--json"]
    )
    ids = {json.loads(l)["id"] for l in out.splitlines() if l.strip()}
    assert ids == {d}


def test_find_with_no_query_match_is_empty(ledger):
    ledger.add("notice", "something")
    code, out, err = ledger.run(["find", "nonexistent-keyword", "--json"])
    assert code == 0
    assert out.strip() == ""


def test_sql_runs_arbitrary_read_only_queries(ledger):
    ledger.add("notice", "n1")
    ledger.add("ask", "q1?")
    code, out, err = ledger.run(
        ["sql", "SELECT kind, count(*) AS n FROM events GROUP BY kind ORDER BY kind"]
    )
    assert code == 0
    assert "ask" in out
    assert "notice" in out

    code, out, err = ledger.run(
        ["sql", "--json", "SELECT id, kind FROM events ORDER BY seq"]
    )
    assert code == 0
    rows = [json.loads(l) for l in out.splitlines() if l.strip()]
    assert rows[0]["kind"] == "notice"
    assert rows[1]["kind"] == "ask"


def test_sql_rejects_writes_via_duckdbs_own_readonly_enforcement(ledger):
    n = ledger.add("notice", "n1")
    before = ledger.db_counts()

    for query in (
        "DELETE FROM events",
        "UPDATE events SET content = 'x'",
        "DROP TABLE events",
        "INSERT INTO events (id, kind, content, actor, occurred_at, recorded_at) "
        "VALUES ('x','notice','x','x', now(), now())",
    ):
        code, out, err = ledger.run(["sql", query])
        assert code == 1, f"expected rejection for: {query}"

    assert ledger.db_counts() == before


def test_sql_reads_from_stdin_with_dash(ledger, monkeypatch):
    ledger.add("notice", "n1")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("SELECT count(*) FROM events"))
    code, out, err = ledger.run(["sql", "-"])
    assert code == 0
    assert "1" in out


def test_sql_requires_existing_ledger(tmp_path):
    empty = tmp_path / "nowhere"
    empty.mkdir()
    code, out, err = run_cli(["sql", "SELECT 1"], empty)
    assert code == 1
    assert "no ledger" in err
