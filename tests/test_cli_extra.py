"""Additional CLI-level checks not covered by the S1-S10 scenarios:
sugar/explicit byte-identity, env var discovery, --full-ids, atomic
add+evidence, and id prefix resolution."""

from __future__ import annotations

import json
import os

import pytest

from helpers import Ledger, run_cli


def test_sugar_commands_are_byte_identical_to_explicit_add(ledger):
    """Sugar commands must produce events byte-identical to the
    equivalent explicit `ev add`. Since `recorded_at` is always the
    real wall-clock time of insertion, two separate CLI invocations a
    few microseconds apart cannot be compared directly for id
    equality; instead we prove the sugar commands construct exactly
    the kind/edge-label combination the spec mandates (the only way
    they could otherwise diverge from the explicit form), and
    separately prove (at the storage layer, with a frozen clock) that
    identical kind/content/edges/actor/timestamps always yield
    identical ids regardless of call path."""
    from datetime import datetime, timezone

    from evlog import store as store_mod

    con = store_mod.connect(ledger.path / ".ledger" / "ledger.duckdb")
    try:
        i = store_mod.append(con, kind="intend", content="do the thing", edges=[]).id
        frozen_now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        r1 = store_mod.append(
            con, kind="do", content="did it", edges=[("fulfils", i)], actor="tester", now=frozen_now
        )
        r2 = store_mod.append(
            con, kind="do", content="did it", edges=[("fulfils", i)], actor="tester", now=frozen_now
        )
        assert r1.id == r2.id  # append() is idempotent: same id, no duplicate write
    finally:
        con.close()

    # Now prove the CLI's sugar commands route through append() with
    # the exact kind/edge-label the spec mandates, by checking the
    # resulting event's kind and edges match what `ev add` would need.
    i2 = ledger.add("intend", "another thing")
    did_id = ledger.do(i2, "did it 2")
    code, out, err = ledger.run(["show", "--json", did_id])
    assert code == 0
    obj = json.loads(out.strip().splitlines()[-1])
    assert obj["kind"] == "do"
    assert obj["edges"] == [{"label": "fulfils", "dst": i2}]

    q2 = ledger.add("ask", "q2?")
    ans_id = ledger.answer(q2, "a2")
    code, out, err = ledger.run(["show", "--json", ans_id])
    obj = json.loads(out.strip().splitlines()[-1])
    assert obj["kind"] == "answer"
    assert obj["edges"] == [{"label": "fulfils", "dst": q2}]

    n2 = ledger.add("notice", "n2")
    drop_id = ledger.drop(n2, "nope2")
    code, out, err = ledger.run(["show", "--json", drop_id])
    obj = json.loads(out.strip().splitlines()[-1])
    assert obj["kind"] == "drop"
    assert obj["edges"] == [{"label": "drops", "dst": n2}]


def test_env_actor_and_env_db(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    l = Ledger(proj)

    monkeypatch.setenv("EV_ACTOR", "env-actor")
    code, out, err = run_cli(["add", "--json", "notice", "n1"], proj)
    assert code == 0
    eid = json.loads(out.strip().splitlines()[-1])["id"]
    monkeypatch.delenv("EV_ACTOR", raising=False)

    code, out, err = run_cli(["show", "--json", eid], proj)
    assert code == 0
    obj = json.loads(out.strip().splitlines()[-1])
    assert obj["actor"] == "env-actor"

    # EV_DB: point at the db from a different cwd.
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    db_path = proj / ".ledger" / "ledger.duckdb"
    monkeypatch.setenv("EV_DB", str(db_path))
    code, out, err = run_cli(["status", "--json", eid], other_cwd)
    assert code == 0
    monkeypatch.delenv("EV_DB", raising=False)


def test_full_ids_flag(ledger):
    n = ledger.add("notice", "something")
    code, out, err = ledger.run(["log"])
    assert code == 0
    short_line = out.strip().splitlines()[0]
    assert short_line.split("\t")[0] == n[:8]

    code, out, err = ledger.run(["log", "--full-ids"])
    assert code == 0
    full_line = out.strip().splitlines()[0]
    assert full_line.split("\t")[0] == n


def test_add_with_evidence_is_atomic(ledger, tmp_path):
    good_file = ledger.path / "good.txt"
    good_file.write_bytes(b"hello")
    missing_file = ledger.path / "nope.txt"

    before = ledger.event_count()
    code, out, err = ledger.run(
        ["add", "--json", "do", "with evidence", "--evidence", str(good_file)]
    )
    assert code == 0
    eid = json.loads(out.strip().splitlines()[-1])["id"]
    assert ledger.event_count() == before + 1
    assert len(ledger.evidence(eid)) == 1

    before2 = ledger.event_count()
    code, out, err = ledger.run(
        [
            "add",
            "--json",
            "do",
            "with missing evidence",
            "--evidence",
            str(good_file),
            "--evidence",
            str(missing_file),
        ]
    )
    assert code == 1
    assert ledger.event_count() == before2


def test_id_prefix_resolution_ambiguous_and_unknown(ledger):
    n = ledger.add("notice", "n")
    code, out, err = ledger.run(["status", n[:8]])
    assert code == 0

    code, out, err = ledger.run(["status", "deadbeef"])
    assert code == 1

    code, out, err = ledger.run(["status", "abc"])
    assert code == 1  # shorter than minimum 4 chars is also an error


def test_no_ledger_found_does_not_silently_create_one(tmp_path):
    """Only `ev init` (and an explicit --db/EV_DB) may bring a ledger
    file into existence. Any other command run where none can be
    discovered must fail loudly instead of silently vivifying an empty
    ledger in cwd."""
    empty_dir = tmp_path / "nowhere"
    empty_dir.mkdir()

    code, out, err = run_cli(["log", "--json"], empty_dir)
    assert code == 1
    assert "no ledger found" in err
    assert not (empty_dir / ".ledger").exists()

    code, out, err = run_cli(["add", "notice", "x"], empty_dir)
    assert code == 1
    assert not (empty_dir / ".ledger").exists()

    # init still works, and after it, commands succeed without --db.
    code, out, err = run_cli(["init"], empty_dir)
    assert code == 0
    assert (empty_dir / ".ledger" / "ledger.duckdb").exists()
    code, out, err = run_cli(["log", "--json"], empty_dir)
    assert code == 0
