"""Property tests (Hypothesis) P1-P11 from section 10.1."""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from evlog import blobs as blobs_mod
from evlog import interchange
from evlog import model
from evlog import reference as ref
from evlog import store as store_mod
from evlog import views as views_mod
from evlog.model import (
    OPENING_KINDS,
    ValidationError,
    compute_event_id,
    compute_evidence_id,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "evlog"

settings.register_profile("ev", max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile("ev")


def make_memory_con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(store_mod.SCHEMA_SQL)
    return con


def db_counts(con) -> dict:
    counts = {}
    for t in ("events", "edges", "evidence", "blobs", "blob_locations"):
        counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    counts["max_seq"] = con.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM (SELECT seq FROM events UNION ALL SELECT seq FROM evidence)"
    ).fetchone()[0]
    return counts


KINDS = ["notice", "ask", "intend", "option", "decide", "do", "answer", "learn"]


def build_random_valid_ledger(con, rnd: random.Random, n_steps: int):
    """Drives store.append with random choices among currently-legal
    edges, skipping anything the validator rejects. Returns the list of
    successfully-created event ids in creation order."""
    created: list[str] = []
    opening_ids: list[str] = []

    for step in range(n_steps):
        kind = rnd.choice(KINDS)
        edges: list[tuple[str, str]] = []

        if created and rnd.random() < 0.35:
            edges.append(("from", rnd.choice(created)))
        if opening_ids and rnd.random() < 0.3:
            edges.append(("needs", rnd.choice(opening_ids)))
        if opening_ids and rnd.random() < 0.3:
            label = rnd.choice(["fulfils", "drops"])
            edges.append((label, rnd.choice(opening_ids)))
        if created and rnd.random() < 0.1:
            edges.append(("retracts", rnd.choice(created)))

        # de-dup (label, dst) pairs
        edges = list(dict.fromkeys(edges))

        try:
            result = store_mod.append(
                con, kind=kind, content=f"event {step}", edges=edges
            )
        except ValidationError:
            continue
        if result.created:
            created.append(result.id)
            if kind in OPENING_KINDS:
                opening_ids.append(result.id)
    return created


# --- P1: hash determinism ----------------------------------------------------


@given(
    actor=st.text(min_size=1, max_size=10),
    content=st.text(min_size=1, max_size=20),
    kind=st.sampled_from(KINDS),
    n_edges=st.integers(min_value=0, max_value=3),
)
def test_p1_hash_determinism(actor, content, kind, n_edges):
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    edges = [("from", f"dst{i:064x}"[:64]) for i in range(n_edges)]

    id1 = compute_event_id(
        actor=actor, content=content, edges=edges, kind=kind, occurred_at=now, recorded_at=now
    )
    id2 = compute_event_id(
        actor=actor, content=content, edges=edges, kind=kind, occurred_at=now, recorded_at=now
    )
    assert id1 == id2

    # edge order doesn't matter
    id3 = compute_event_id(
        actor=actor, content=content, edges=list(reversed(edges)), kind=kind,
        occurred_at=now, recorded_at=now,
    )
    assert id1 == id3

    # changing content changes id
    id4 = compute_event_id(
        actor=actor, content=content + "!", edges=edges, kind=kind, occurred_at=now, recorded_at=now
    )
    assert id4 != id1

    # changing actor changes id
    id5 = compute_event_id(
        actor=actor + "!", content=content, edges=edges, kind=kind, occurred_at=now, recorded_at=now
    )
    assert id5 != id1

    # changing recorded_at changes id
    id6 = compute_event_id(
        actor=actor, content=content, edges=edges, kind=kind, occurred_at=now,
        recorded_at=now + timedelta(seconds=1),
    )
    assert id6 != id1

    # evidence ids: determinism and order-insensitivity trivially (no list)
    ev1 = compute_evidence_id(
        actor=actor, blob_hash="a" * 64, event_id="b" * 64, note=content, recorded_at=now
    )
    ev2 = compute_evidence_id(
        actor=actor, blob_hash="a" * 64, event_id="b" * 64, note=content, recorded_at=now
    )
    assert ev1 == ev2
    ev3 = compute_evidence_id(
        actor=actor, blob_hash="c" * 64, event_id="b" * 64, note=content, recorded_at=now
    )
    assert ev3 != ev1


# --- P2: acyclic by construction ----------------------------------------------


@given(seed=st.integers(min_value=0, max_value=2**31 - 1), n_steps=st.integers(min_value=5, max_value=30))
def test_p2_acyclic_by_construction(seed, n_steps):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        build_random_valid_ledger(con, rnd, n_steps)
        events = store_mod.fetch_events(con)
        events_by_id = {e.id: e for e in events}
        for e in events:
            for edge in e.edges:
                target = events_by_id[edge.dst]
                assert target.seq < e.seq

        report = interchange.verify(con)
        assert report.ok, report.issues
    finally:
        con.close()


# --- P3: append-only (static source scan) -------------------------------------


def test_p3_no_mutating_sql_in_source():
    # Match SQL statements specifically (not Python method calls like
    # hashlib's h.update(...)): require the keyword not be preceded by
    # a '.' and, for UPDATE, be followed by whitespace+identifier (SQL
    # table reference) rather than an immediate '('.
    update_re = re.compile(r"(?<!\.)\bUPDATE\s+[A-Za-z_]", re.IGNORECASE)
    delete_re = re.compile(r"(?<!\.)\bDELETE\s+FROM\b", re.IGNORECASE)
    drop_re = re.compile(r"(?<!\.)\bDROP\s+(TABLE|SEQUENCE)\b", re.IGNORECASE)
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for regex in (update_re, delete_re, drop_re):
            for m in regex.finditer(text):
                pytest.fail(f"found forbidden SQL statement {m.group(0)!r} in {path}")


@given(seed=st.integers(min_value=0, max_value=2**31 - 1), n_steps=st.integers(min_value=5, max_value=20))
def test_p3_append_only_counts_only_grow(seed, n_steps):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        prev_counts = db_counts(con)
        for _ in range(n_steps):
            build_random_valid_ledger(con, rnd, 1)
            new_counts = db_counts(con)
            for k, v in new_counts.items():
                assert v >= prev_counts[k]
            prev_counts = new_counts
    finally:
        con.close()


# --- P4: replay ----------------------------------------------------------------


@given(seed=st.integers(min_value=0, max_value=2**31 - 1), n_steps=st.integers(min_value=5, max_value=25))
def test_p4_replay_export_import(seed, n_steps):
    rnd = random.Random(seed)
    con_a = make_memory_con()
    con_b = make_memory_con()
    try:
        build_random_valid_ledger(con_a, rnd, n_steps)

        lines = interchange.export_lines(con_a)
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
            tmp_path = Path(f.name)
        try:
            interchange.import_from(con_b, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        events_a = store_mod.fetch_events(con_a)
        events_b = store_mod.fetch_events(con_b)
        ids_a = {e.id for e in events_a}
        ids_b = {e.id for e in events_b}
        assert ids_a == ids_b

        ev_a = store_mod.fetch_evidence(con_a)
        ev_b = store_mod.fetch_evidence(con_b)
        assert {e.id for e in ev_a} == {e.id for e in ev_b}

        statuses_a = ref.compute_status_all(events_a, ev_a, ref.ViewSelector())
        statuses_b = ref.compute_status_all(events_b, ev_b, ref.ViewSelector())
        assert statuses_a == statuses_b
    finally:
        con_a.close()
        con_b.close()


# --- P5: differential (views.py vs reference.py) -------------------------------


@given(seed=st.integers(min_value=0, max_value=2**31 - 1), n_steps=st.integers(min_value=5, max_value=25))
def test_p5_differential_status(seed, n_steps):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        created = build_random_valid_ledger(con, rnd, n_steps)
        if not created:
            return
        events, evidence = views_mod.load_ledger(con)

        selectors = [ref.ViewSelector()]
        if events:
            mid = events[len(events) // 2]
            selectors.append(ref.ViewSelector(as_of_seq=mid.seq))
            selectors.append(ref.ViewSelector(line=rnd.choice(created)))

        for selector in selectors:
            db_statuses = views_mod.statuses_in_view(con, selector)
            ref_statuses = ref.compute_status_all(events, evidence, selector)
            assert db_statuses == ref_statuses
    finally:
        con.close()


# --- P6: rewind equals prefix replay (pure reference-model check) --------------


@given(seed=st.integers(min_value=0, max_value=2**31 - 1), n_steps=st.integers(min_value=5, max_value=25))
def test_p6_rewind_equals_prefix_replay(seed, n_steps):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        build_random_valid_ledger(con, rnd, n_steps)
        events, evidence = views_mod.load_ledger(con)
        if not events:
            return
        for n in (1, len(events) // 2 + 1, len(events)):
            full_as_of = ref.compute_status_all(events, evidence, ref.ViewSelector(as_of_seq=n))
            prefix_events = [e for e in events if e.seq <= n]
            prefix_evidence = [e for e in evidence if e.seq <= n]
            prefix_only = ref.compute_status_all(prefix_events, prefix_evidence, ref.ViewSelector())
            assert full_as_of == prefix_only
    finally:
        con.close()


# --- P7: merge is union --------------------------------------------------------


@given(seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_p7_merge_is_union(seed):
    rnd = random.Random(seed)
    con_a = make_memory_con()
    con_b = make_memory_con()
    try:
        shared = build_random_valid_ledger(con_a, rnd, 10)

        import tempfile

        lines = interchange.export_lines(con_a)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
            shared_path = Path(f.name)
        try:
            interchange.import_from(con_b, shared_path)
        finally:
            shared_path.unlink(missing_ok=True)

        build_random_valid_ledger(con_a, rnd, 8)
        build_random_valid_ledger(con_b, rnd, 8)

        def export_tmp(con):
            lines = interchange.export_lines(con)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
                return Path(f.name)

        path_a = export_tmp(con_a)
        path_b = export_tmp(con_b)
        try:
            interchange.import_from(con_b, path_a)
            interchange.import_from(con_a, path_b)
        finally:
            path_a.unlink(missing_ok=True)
            path_b.unlink(missing_ok=True)

        ids_a = {e.id for e in store_mod.fetch_events(con_a)}
        ids_b = {e.id for e in store_mod.fetch_events(con_b)}
        assert ids_a == ids_b

        statuses_a = ref.compute_status_all(*views_mod.load_ledger(con_a))
        statuses_b = ref.compute_status_all(*views_mod.load_ledger(con_b))
        assert statuses_a == statuses_b

        counts_before = db_counts(con_a)
        path_b2 = export_tmp(con_b)
        try:
            interchange.import_from(con_a, path_b2)
        finally:
            path_b2.unlink(missing_ok=True)
        counts_after = db_counts(con_a)
        assert counts_before == counts_after
    finally:
        con_a.close()
        con_b.close()


# --- P8: rejection is clean -----------------------------------------------------


def test_p8_rejection_is_clean_bad_kind():
    con = make_memory_con()
    try:
        rnd = random.Random(1)
        build_random_valid_ledger(con, rnd, 10)
        before = db_counts(con)
        with pytest.raises(ValidationError):
            store_mod.append(con, kind="Bad Kind", content="x", edges=[])
        assert db_counts(con) == before
    finally:
        con.close()


def test_p8_rejection_is_clean_double_close():
    con = make_memory_con()
    try:
        n = store_mod.append(con, kind="notice", content="n", edges=[]).id
        before = db_counts(con)
        with pytest.raises(ValidationError):
            store_mod.append(
                con, kind="do", content="x", edges=[("fulfils", n), ("drops", n)]
            )
        assert db_counts(con) == before
    finally:
        con.close()


def test_p8_rejection_is_clean_attach_missing_event():
    con = make_memory_con()
    try:
        before = db_counts(con)
        with pytest.raises(ValidationError):
            blobs_mod.attach_evidence_row(
                con, event_id="deadbeef" * 8, blob_hash="a" * 64, note=""
            )
        assert db_counts(con) == before
    finally:
        con.close()


# --- P9: evidence is inert -------------------------------------------------------


@given(seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_p9_evidence_is_inert(seed):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        created = build_random_valid_ledger(con, rnd, 15)
        if not created:
            return

        before = ref.compute_status_all(*views_mod.load_ledger(con))

        con.execute(
            "INSERT INTO blobs (hash, media_type, size, recorded_at) VALUES (?, ?, ?, ?)",
            ["f" * 64, "text/plain", 5, model.utcnow()],
        )
        for eid in created[:5]:
            blobs_mod.attach_evidence_row(
                con, event_id=eid, blob_hash="f" * 64, note="note"
            )

        after = ref.compute_status_all(*views_mod.load_ledger(con))
        assert before == after

        for selector in [ref.ViewSelector(), ref.ViewSelector(line=created[-1])]:
            s1 = ref.compute_status_all(*views_mod.load_ledger(con), selector)
            assert s1 == ref.compute_status_all(*views_mod.load_ledger(con), selector)
    finally:
        con.close()


# --- P10: evidence idempotent & ordered -------------------------------------------


@given(seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_p10_evidence_idempotent_and_ordered(seed):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        created = build_random_valid_ledger(con, rnd, 10)
        if not created:
            return
        con.execute(
            "INSERT INTO blobs (hash, media_type, size, recorded_at) VALUES (?, ?, ?, ?)",
            ["e" * 64, "text/plain", 5, model.utcnow()],
        )
        eid = created[0]
        _h1, id1 = blobs_mod.attach_evidence_row(
            con, event_id=eid, blob_hash="e" * 64, note="x", actor="a", now=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )
        before = db_counts(con)
        _h2, id2 = blobs_mod.attach_evidence_row(
            con, event_id=eid, blob_hash="e" * 64, note="x", actor="a", now=datetime(2024, 1, 1, tzinfo=timezone.utc)
        )
        assert id1 == id2
        assert db_counts(con) == before

        all_seqs = sorted(
            [e.seq for e in store_mod.fetch_events(con)]
            + [e.seq for e in store_mod.fetch_evidence(con)]
        )
        assert all_seqs == list(range(1, len(all_seqs) + 1))
    finally:
        con.close()


# --- P11: identity versus location ------------------------------------------------


def test_p11_relocate_does_not_change_ids_or_status():
    con = make_memory_con()
    try:
        rnd = random.Random(3)
        created = build_random_valid_ledger(con, rnd, 10)
        con.execute(
            "INSERT INTO blobs (hash, media_type, size, recorded_at) VALUES (?, ?, ?, ?)",
            ["d" * 64, "text/plain", 5, model.utcnow()],
        )
        before_ids = {e.id for e in store_mod.fetch_events(con)}
        before_status = ref.compute_status_all(*views_mod.load_ledger(con))

        blobs_mod.relocate_blob(con, "d" * 64, "s3://bucket/key")

        after_ids = {e.id for e in store_mod.fetch_events(con)}
        after_status = ref.compute_status_all(*views_mod.load_ledger(con))
        assert before_ids == after_ids
        assert before_status == after_status
    finally:
        con.close()


def test_p11_corrupted_blob_fails_verify_blobs(tmp_path):
    db_path = tmp_path / ".ledger" / "ledger.duckdb"
    con = store_mod.connect(db_path)
    try:
        n = store_mod.append(con, kind="notice", content="n", edges=[]).id
        bs = blobs_mod.blob_store_for_db(db_path)
        h, _evid = blobs_mod.attach_file(
            con, bs, n, _write_tmp_file(tmp_path, b"hello")
        )
        report = interchange.verify(con, check_blobs=True, blob_store=bs)
        assert report.ok

        bs._path_for(h).write_bytes(b"corrupted!!")
        report2 = interchange.verify(con, check_blobs=True, blob_store=bs)
        assert not report2.ok
        assert any("mismatch" in i for i in report2.blob_issues)

        bs._path_for(h).unlink()
        report3 = interchange.verify(con, check_blobs=True, blob_store=bs)
        assert report3.ok
        assert any("unavailable" in i for i in report3.blob_issues)
    finally:
        con.close()


def _write_tmp_file(tmp_path, data: bytes) -> Path:
    p = tmp_path / "evidence_src.bin"
    p.write_bytes(data)
    return p


# --- P12 (new): time_marks are inert, append-only, idempotent, and share seq --


@given(seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_p12_time_marks_are_inert(seed):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        created = build_random_valid_ledger(con, rnd, 15)
        if not created:
            return

        before = ref.compute_status_all(*views_mod.load_ledger(con))

        for i, eid in enumerate(created[:5]):
            # mix of past and future instants -- both must be inert
            offset_days = -30 if i % 2 == 0 else 365
            at = (model.utcnow() + __import__("datetime").timedelta(days=offset_days))
            store_mod.mark_time(con, event_id=eid, at=model.format_timestamp(at))

        after = ref.compute_status_all(*views_mod.load_ledger(con))
        assert before == after
    finally:
        con.close()


@given(seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_p12_time_marks_future_dates_are_never_rejected(seed):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        created = build_random_valid_ledger(con, rnd, 5)
        if not created:
            return
        far_future = "2099-01-01T00:00:00Z"
        for eid in created:
            # must not raise
            store_mod.mark_time(con, event_id=eid, at=far_future)
    finally:
        con.close()


def test_p12_time_marks_rejection_is_clean():
    con = make_memory_con()
    try:
        before = db_counts(con)
        with pytest.raises(ValidationError):
            store_mod.mark_time(con, event_id="deadbeef" * 8, at="2030-01-01T00:00:00Z")
        assert db_counts(con) == before

        n = store_mod.append(con, kind="notice", content="n", edges=[]).id
        before2 = db_counts(con)
        with pytest.raises(ValidationError):
            store_mod.mark_time(con, event_id=n, at="2030-01-01T00:00:00")  # naive
        assert db_counts(con) == before2
    finally:
        con.close()


def test_p12_time_marks_idempotent_and_share_seq():
    con = make_memory_con()
    try:
        n = store_mod.append(con, kind="notice", content="n", edges=[]).id
        frozen_now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        r1 = store_mod.mark_time(
            con, event_id=n, at="2030-01-01T00:00:00Z", now=frozen_now, actor="a"
        )
        before = db_counts(con)
        r2 = store_mod.mark_time(
            con, event_id=n, at="2030-01-01T00:00:00Z", now=frozen_now, actor="a"
        )
        assert r1.id == r2.id
        assert db_counts(con) == before

        all_seqs = sorted(
            [e.seq for e in store_mod.fetch_events(con)]
            + [e.seq for e in store_mod.fetch_evidence(con)]
            + [m.seq for m in store_mod.fetch_time_marks(con)]
        )
        assert all_seqs == list(range(1, len(all_seqs) + 1))
    finally:
        con.close()


@given(seed=st.integers(min_value=0, max_value=2**31 - 1))
def test_p12_latest_time_mark_wins_and_is_view_relative(seed):
    rnd = random.Random(seed)
    con = make_memory_con()
    try:
        created = build_random_valid_ledger(con, rnd, 5)
        if not created:
            return
        eid = created[0]

        r1 = store_mod.mark_time(con, event_id=eid, at="2030-01-01T00:00:00Z")
        events, _ = views_mod.load_ledger(con)
        marks_after_first = store_mod.fetch_time_marks(con)
        seq_after_first = max(m.seq for m in marks_after_first)

        r2 = store_mod.mark_time(con, event_id=eid, at="2031-01-01T00:00:00Z")
        all_marks = store_mod.fetch_time_marks(con)

        current_now = ref.current_time_marks(all_marks)
        assert current_now[eid].at == model.parse_strict_iso8601("2031-01-01T00:00:00Z")

        visible_then = ref.visible_time_marks(
            all_marks, {eid}, ref.ViewSelector(as_of_seq=seq_after_first)
        )
        current_then = ref.current_time_marks(visible_then)
        assert current_then[eid].at == model.parse_strict_iso8601("2030-01-01T00:00:00Z")
    finally:
        con.close()
