"""Scenario tests S1-S10 from section 10.2, run through the real CLI."""

from __future__ import annotations

import concurrent.futures
import time

import pytest

from evlog import store as store_mod


def test_s1_basic_lifecycle_and_rewind(ledger):
    n = ledger.add("notice", "legacy simulator is too slow")
    d = ledger.add("decide", "rebuild with parallel execution", fulfils=[n])
    i = ledger.add("intend", "implement the parallel simulator", **{"from": [d]})
    x = ledger.add("do", "first parallel run matches legacy output", fulfils=[i])

    assert ledger.status(n) == "fulfilled"
    assert ledger.status(i) == "fulfilled"
    assert ledger.status(n, as_of=ledger.seq(n)) == "ready"
    assert ledger.status(i, as_of=ledger.seq(d)) is None
    assert ledger.status(i, as_of=ledger.seq(i)) == "ready"


def test_s2_needing_information(ledger):
    q = ledger.add("ask", "is JAX or torch better for this model?")
    i = ledger.add("intend", "port the simulator", needs=[q])
    assert ledger.status(q) == "ready"
    assert ledger.status(i) == "blocked"
    assert ledger.ids("ls", "--status", "ready") == {q}

    ledger.add("answer", "JAX; 3x faster in the benchmark", fulfils=[q])
    assert ledger.status(q) == "fulfilled"
    assert ledger.status(i) == "ready"


def test_s3_dropped_prerequisite_then_supersede(ledger):
    a = ledger.add("intend", "provision the A100")
    b = ledger.add("intend", "run the benchmark", needs=[a])
    ledger.add("drop", "budget refused", drops=[a])
    assert ledger.status(a) == "dropped"
    assert ledger.status(b) == "stuck"
    assert ledger.ids("ls", "--status", "stuck") == {b}
    assert b not in ledger.ids("ls", "--status", "ready")

    b2 = ledger.supersede(b, "intend", "run the benchmark on the laptop")
    assert ledger.status(b) == "dropped"
    assert ledger.status(b2) == "ready"
    assert b in ledger.ids("why", b2)


def test_s4_retraction_rewrites_present_not_past(ledger):
    i = ledger.add("intend", "regenerate the report")
    x = ledger.add("do", "regenerated report", fulfils=[i])
    r = ledger.add("retract", "wrong input file", retracts=[x])

    assert ledger.status(i) == "ready"
    assert ledger.status(i, as_of=ledger.seq(x)) == "fulfilled"
    assert ledger.status(i, as_of=ledger.seq(r)) == "ready"

    ledger.add("retract", "it was the right file after all", retracts=[r])
    assert ledger.status(i) == "fulfilled"


def test_s5_alternate_futures(ledger):
    q = ledger.add("ask", "which storage engine?")
    o1 = ledger.add("option", "DuckDB", **{"from": [q]})
    o2 = ledger.add("option", "Postgres", **{"from": [q]})
    d1 = ledger.add("decide", "DuckDB", **{"from": [o1]}, fulfils=[q])
    i1 = ledger.add("intend", "write the schema", **{"from": [d1]})
    d2 = ledger.add("decide", "Postgres", **{"from": [o2]}, fulfils=[q])
    i2 = ledger.add("intend", "provision a server", **{"from": [d2]})

    assert ledger.status(q) == "contested"
    assert ledger.status(q, line=i1) == "fulfilled"
    assert i2 not in ledger.ids("log", line=i1)
    assert ledger.status(q, line=i2) == "fulfilled"
    assert i1 not in ledger.ids("log", line=i2)

    only_a, only_b = ledger.diff(i1, i2)
    def short(s):
        return {x[:8] for x in s}
    assert short(only_a) == {o1[:8], d1[:8], i1[:8]}
    assert short(only_b) == {o2[:8], d2[:8], i2[:8]}

    assert ledger.ids("tips") == {i1, i2}

    code, out, err = ledger.reject("add", "decide", "changed my mind", "--from", d1, "--fulfils", q)
    assert code == 1

    ledger.retract(d2, "going with DuckDB", descendants=True)
    assert ledger.status(q) == "fulfilled"
    assert ledger.ids("ls", "--status", "ready") == {i1}
    assert ledger.status(q, as_of=ledger.seq(i2)) == "contested"


def test_s6_rejections_are_specific_and_leave_no_trace(ledger):
    n = ledger.add("notice", "something")
    x = ledger.add("do", "did something", fulfils=[])  # just a non-opening event

    before = ledger.event_count()
    code, out, err = ledger.reject("status", "deadbeef")
    assert code == 1
    assert ledger.event_count() == before

    code, out, err = ledger.reject("add", "Bad Kind", "text")
    assert code != 0
    assert ledger.event_count() == before

    code, out, err = ledger.reject("add", "intend", "text", "--needs", x)
    assert code == 1
    assert ledger.event_count() == before

    q = ledger.add("ask", "q?")
    a = ledger.add("answer", "a", fulfils=[q])
    code, out, err = ledger.reject(
        "add", "answer", "a2", "--fulfils", q, "--from", a
    )
    assert code == 1
    assert ledger.event_count() == before + 2

    before2 = ledger.event_count()
    opener = ledger.add("notice", "dual close target")
    code, out, err = ledger.reject(
        "add", "do", "closes both ways", "--fulfils", opener, "--drops", opener
    )
    assert code == 1
    assert ledger.event_count() == before2 + 1

    code, out, err = ledger.reject("add", "do", "future", "--at", "2999-01-01T00:00:00Z")
    assert code == 1

    code, out, err = ledger.reject("add", "do", "naive", "--at", "2020-01-01T00:00:00")
    assert code == 1

    n2 = ledger.add("notice", "retract target")
    r2 = ledger.add("retract", "oops", retracts=[n2])
    code, out, err = ledger.reject(
        "add", "retract", "again", "--retracts", n2, "--from", r2
    )
    assert code == 1


def test_s9_evidence_arrives_late_and_stays_inert(ledger):
    i = ledger.add("intend", "benchmark the parallel simulator")
    x = ledger.add("do", "benchmark finished", fulfils=[i])
    before = ledger.all_statuses()

    h = ledger.attach(x, b"timings: 1 thread 41.2s, 8 threads 6.1s", note="raw timings")
    assert ledger.all_statuses() == before
    assert [e["blob_hash"] for e in ledger.evidence(x)] == [h]
    assert ledger.evidence(x, as_of=ledger.seq(x)) == []

    ids_before = ledger.ids("log")
    ledger.relocate(h, "s3://team-bucket/bench/results.csv")
    assert ledger.ids("log") == ids_before
    assert [e["blob_hash"] for e in ledger.evidence(x)] == [h]


def test_s10_evidence_cannot_dangle(ledger, tmp_path):
    code, out, err = ledger.reject("attach", "deadbeef", str(tmp_path / "nope.txt"))
    assert code == 1

    missing = ledger.path / "missing.csv"
    before = ledger.event_count()
    code, out, err = ledger.reject("add", "do", "text", "--evidence", str(missing))
    assert code == 1
    assert ledger.event_count() == before

    x = ledger.add("notice", "n")
    h = ledger.attach(x, b"hello world")
    from evlog import blobs as blobs_mod
    from evlog import store as store_mod

    bs = blobs_mod.blob_store_for_db(ledger.path / ".ledger" / "ledger.duckdb")
    blob_path = bs._path_for(h)
    blob_path.write_bytes(b"tampered")

    code, out, err = ledger.run(["verify"])
    assert code == 0

    code, out, err = ledger.run(["verify", "--blobs"])
    assert code == 3

    blob_path.unlink()
    code, out, err = ledger.run(["verify", "--blobs"])
    assert code == 0
    assert "unavailable" in out

    out_path = ledger.path / "out.bin"
    code, out, err = ledger.run(["blob", "get", h, "--out", str(out_path)])
    assert code == 1


def test_s7_agents_sharing_a_ledger(ledger):
    db_path = ledger.path / ".ledger" / "ledger.duckdb"

    def worker(actor_name: str, n: int):
        ok = 0
        for i in range(n):
            con = store_mod.connect(db_path)
            try:
                store_mod.append(
                    con,
                    kind="notice",
                    content=f"{actor_name} item {i}",
                    edges=[],
                    actor=actor_name,
                )
                ok += 1
            except store_mod.LedgerBusyError:
                pass
            finally:
                con.close()
        return ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(worker, "agent1", 50)
        f2 = ex.submit(worker, "agent2", 50)
        done1 = f1.result()
        done2 = f2.result()

    assert done1 == 50
    assert done2 == 50
    assert ledger.event_count() == 100
    code, out, err = ledger.run(["verify"])
    assert code == 0


def test_s8_merging_ledgers(tmp_path):
    from helpers import Ledger

    a = Ledger(tmp_path / "a")
    b = Ledger(tmp_path / "b")

    shared = a.add("notice", "shared notice")

    export_a1 = tmp_path / "a1.jsonl"
    a.run(["export", str(export_a1)])
    code, out, err = b.run(["import", str(export_a1)])
    assert code == 0

    a_extra = a.add("decide", "A's decision", fulfils=[shared])
    b_extra = b.add("drop", "B's drop", drops=[shared])

    export_a2 = tmp_path / "a2.jsonl"
    export_b2 = tmp_path / "b2.jsonl"
    a.run(["export", str(export_a2)])
    b.run(["export", str(export_b2)])

    code, out, err = b.run(["import", str(export_a2)])
    assert code == 0
    code, out, err = a.run(["import", str(export_b2)])
    assert code == 0

    a_ids = a.ids("log", "--full-ids")
    b_ids = b.ids("log", "--full-ids")
    assert a_ids == b_ids
    assert {shared, a_extra, b_extra} <= a_ids

    a_statuses = a.all_statuses()
    b_statuses = b.all_statuses()
    assert a_statuses == b_statuses

    before_counts = a.db_counts()
    code, out, err = a.run(["import", str(export_b2)])
    assert code == 0
    after_counts = a.db_counts()
    assert before_counts == after_counts

    for ev_dict in a.evidence(shared):
        pass  # no evidence attached in this scenario; placeholder for symmetry
