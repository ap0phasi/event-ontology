"""Export/import (JSONL interchange) and offline integrity verification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .model import (
    EDGE_LABELS,
    OPENING_KINDS,
    ValidationError,
    compute_event_id,
    compute_evidence_id,
    compute_time_mark_id,
    format_timestamp,
    parse_any_timestamp,
    KIND_PATTERN,
)
from .reference import ViewSelector, compute_live, compute_status_all
from .store import fetch_events, fetch_evidence, fetch_time_marks


# --- Export -----------------------------------------------------------------


def export_lines(con: duckdb.DuckDBPyConnection) -> list[str]:
    lines: list[str] = []

    blobs = con.execute(
        "SELECT hash, media_type, size, recorded_at FROM blobs ORDER BY hash"
    ).fetchall()
    for hash_, media_type, size, recorded_at in blobs:
        obj = {
            "type": "blob",
            "hash": hash_,
            "media_type": media_type,
            "size": size,
            "recorded_at": format_timestamp(parse_any_timestamp(recorded_at)),
        }
        lines.append(_dump(obj))

    locations = con.execute(
        "SELECT hash, uri, recorded_at FROM blob_locations ORDER BY hash, uri, recorded_at"
    ).fetchall()
    for hash_, uri, recorded_at in locations:
        obj = {
            "type": "blob_location",
            "hash": hash_,
            "uri": uri,
            "recorded_at": format_timestamp(parse_any_timestamp(recorded_at)),
        }
        lines.append(_dump(obj))

    events = fetch_events(con)
    evidence = fetch_evidence(con)
    time_marks = fetch_time_marks(con)

    interleaved = (
        [("event", e.seq, e) for e in events]
        + [("evidence", ev.seq, ev) for ev in evidence]
        + [("time_mark", m.seq, m) for m in time_marks]
    )
    interleaved.sort(key=lambda t: t[1])

    for kind, _seq, obj_ in interleaved:
        if kind == "event":
            e = obj_
            rec = {
                "type": "event",
                "id": e.id,
                "kind": e.kind,
                "content": e.content,
                "actor": e.actor,
                "occurred_at": format_timestamp(e.occurred_at),
                "recorded_at": format_timestamp(e.recorded_at),
                "edges": sorted(
                    [{"label": ed.label, "dst": ed.dst} for ed in e.edges],
                    key=lambda d: (d["label"], d["dst"]),
                ),
            }
        elif kind == "evidence":
            ev = obj_
            rec = {
                "type": "evidence",
                "id": ev.id,
                "event_id": ev.event_id,
                "blob_hash": ev.blob_hash,
                "note": ev.note,
                "actor": ev.actor,
                "recorded_at": format_timestamp(ev.recorded_at),
            }
        else:
            m = obj_
            rec = {
                "type": "time_mark",
                "id": m.id,
                "event_id": m.event_id,
                "at": format_timestamp(m.at),
                "note": m.note,
                "actor": m.actor,
                "recorded_at": format_timestamp(m.recorded_at),
            }
        lines.append(_dump(rec))

    return lines


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def export_to(con: duckdb.DuckDBPyConnection, path: Path | None) -> str:
    lines = export_lines(con)
    text = "\n".join(lines) + ("\n" if lines else "")
    if path is not None:
        Path(path).write_text(text, encoding="utf-8")
    return text


# --- Import -----------------------------------------------------------------


@dataclass
class ImportStats:
    blobs_added: int = 0
    locations_added: int = 0
    events_added: int = 0
    evidence_added: int = 0
    time_marks_added: int = 0
    skipped: int = 0


def import_from(con: duckdb.DuckDBPyConnection, path: Path) -> ImportStats:
    text = Path(path).read_text(encoding="utf-8")
    stats = ImportStats()
    known_event_ids: set[str] = set(
        r[0] for r in con.execute("SELECT id FROM events").fetchall()
    )
    known_blob_hashes: set[str] = set(
        r[0] for r in con.execute("SELECT hash FROM blobs").fetchall()
    )

    con.execute("BEGIN TRANSACTION")
    try:
        for lineno, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            t = obj.get("type")
            if t == "blob":
                _import_blob(con, obj, known_blob_hashes, stats)
            elif t == "blob_location":
                _import_blob_location(con, obj, stats)
            elif t == "event":
                _import_event(con, obj, known_event_ids, stats)
            elif t == "evidence":
                _import_evidence(con, obj, known_event_ids, known_blob_hashes, stats)
            elif t == "time_mark":
                _import_time_mark(con, obj, known_event_ids, stats)
            else:
                raise ValidationError(f"unknown row type {t!r} at line {lineno}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return stats


def _import_blob(con, obj, known_blob_hashes, stats: ImportStats):
    hash_ = obj["hash"]
    if hash_ in known_blob_hashes:
        stats.skipped += 1
        return
    recorded_at = parse_any_timestamp(obj["recorded_at"])
    con.execute(
        "INSERT INTO blobs (hash, media_type, size, recorded_at) VALUES (?, ?, ?, ?)",
        [hash_, obj["media_type"], obj["size"], recorded_at],
    )
    known_blob_hashes.add(hash_)
    stats.blobs_added += 1


def _import_blob_location(con, obj, stats: ImportStats):
    hash_ = obj["hash"]
    uri = obj["uri"]
    recorded_at = parse_any_timestamp(obj["recorded_at"])
    existing = con.execute(
        "SELECT 1 FROM blob_locations WHERE hash = ? AND uri = ? AND recorded_at = ?",
        [hash_, uri, recorded_at],
    ).fetchone()
    if existing is not None:
        stats.skipped += 1
        return
    con.execute(
        "INSERT INTO blob_locations (hash, uri, recorded_at) VALUES (?, ?, ?)",
        [hash_, uri, recorded_at],
    )
    stats.locations_added += 1


def _import_event(con, obj, known_event_ids, stats: ImportStats):
    id_ = obj["id"]
    if id_ in known_event_ids:
        stats.skipped += 1
        return
    kind = obj["kind"]
    content = obj["content"]
    actor = obj["actor"]
    occurred_at = parse_any_timestamp(obj["occurred_at"])
    recorded_at = parse_any_timestamp(obj["recorded_at"])
    edges = [(ed["label"], ed["dst"]) for ed in obj.get("edges", [])]

    for _label, dst in edges:
        if dst not in known_event_ids:
            raise ValidationError(
                f"event {id_[:8]} references unknown edge target {dst[:8]}"
            )

    recomputed = compute_event_id(
        actor=actor,
        content=content,
        edges=edges,
        kind=kind,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
    )
    if recomputed != id_:
        raise ValidationError(
            f"event {id_[:8]} fails id recomputation (tampered interchange file)"
        )

    con.execute(
        "INSERT INTO events (id, kind, content, actor, occurred_at, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [id_, kind, content, actor, occurred_at, recorded_at],
    )
    for label, dst in edges:
        con.execute(
            "INSERT INTO edges (src, dst, label) VALUES (?, ?, ?)", [id_, dst, label]
        )
    known_event_ids.add(id_)
    stats.events_added += 1


def _import_evidence(con, obj, known_event_ids, known_blob_hashes, stats: ImportStats):
    id_ = obj["id"]
    existing = con.execute("SELECT 1 FROM evidence WHERE id = ?", [id_]).fetchone()
    if existing is not None:
        stats.skipped += 1
        return
    event_id = obj["event_id"]
    blob_hash = obj["blob_hash"]
    note = obj.get("note", "")
    actor = obj["actor"]
    recorded_at = parse_any_timestamp(obj["recorded_at"])

    if event_id not in known_event_ids:
        raise ValidationError(
            f"evidence {id_[:8]} references unknown event {event_id[:8]}"
        )
    if blob_hash not in known_blob_hashes:
        raise ValidationError(
            f"evidence {id_[:8]} references unknown blob {blob_hash[:8]}"
        )

    recomputed = compute_evidence_id(
        actor=actor, blob_hash=blob_hash, event_id=event_id, note=note, recorded_at=recorded_at
    )
    if recomputed != id_:
        raise ValidationError(
            f"evidence {id_[:8]} fails id recomputation (tampered interchange file)"
        )

    con.execute(
        "INSERT INTO evidence (id, event_id, blob_hash, note, actor, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [id_, event_id, blob_hash, note, actor, recorded_at],
    )
    stats.evidence_added += 1


def _import_time_mark(con, obj, known_event_ids, stats: ImportStats):
    id_ = obj["id"]
    existing = con.execute(
        "SELECT 1 FROM time_marks WHERE id = ?", [id_]
    ).fetchone()
    if existing is not None:
        stats.skipped += 1
        return
    event_id = obj["event_id"]
    at = parse_any_timestamp(obj["at"])
    note = obj.get("note", "")
    actor = obj["actor"]
    recorded_at = parse_any_timestamp(obj["recorded_at"])

    if event_id not in known_event_ids:
        raise ValidationError(
            f"time_mark {id_[:8]} references unknown event {event_id[:8]}"
        )

    recomputed = compute_time_mark_id(
        actor=actor, at=at, event_id=event_id, note=note, recorded_at=recorded_at
    )
    if recomputed != id_:
        raise ValidationError(
            f"time_mark {id_[:8]} fails id recomputation (tampered interchange file)"
        )

    con.execute(
        "INSERT INTO time_marks (id, event_id, at_time, note, actor, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [id_, event_id, at, note, actor, recorded_at],
    )
    stats.time_marks_added += 1


# --- Verify -------------------------------------------------------------------


@dataclass
class VerifyReport:
    ok: bool
    issues: list[str]
    blob_issues: list[str]


def verify(con: duckdb.DuckDBPyConnection, check_blobs: bool = False, blob_store=None) -> VerifyReport:
    issues: list[str] = []

    events = fetch_events(con)
    evidence = fetch_evidence(con)
    time_marks = fetch_time_marks(con)
    events_by_id = {e.id: e for e in events}
    events_by_seq = {e.id: e.seq for e in events}

    # Recompute event ids.
    for e in events:
        recomputed = compute_event_id(
            actor=e.actor,
            content=e.content,
            edges=[(ed.label, ed.dst) for ed in e.edges],
            kind=e.kind,
            occurred_at=e.occurred_at,
            recorded_at=e.recorded_at,
        )
        if recomputed != e.id:
            issues.append(f"event {e.id[:8]} id does not match its stored fields")
        if not KIND_PATTERN.match(e.kind):
            issues.append(f"event {e.id[:8]} has invalid kind {e.kind!r}")

    # Recompute evidence ids.
    for ev in evidence:
        recomputed = compute_evidence_id(
            actor=ev.actor,
            blob_hash=ev.blob_hash,
            event_id=ev.event_id,
            note=ev.note,
            recorded_at=ev.recorded_at,
        )
        if recomputed != ev.id:
            issues.append(f"evidence {ev.id[:8]} id does not match its stored fields")

    # Recompute time_mark ids and referential/ordering checks. Note:
    # unlike events, a TimeMark's `at` is deliberately unconstrained
    # (future-dated marks are the whole point) -- only recorded_at is
    # checked against its event, same clamp as evidence.
    for m in time_marks:
        recomputed = compute_time_mark_id(
            actor=m.actor, at=m.at, event_id=m.event_id, note=m.note,
            recorded_at=m.recorded_at,
        )
        if recomputed != m.id:
            issues.append(f"time_mark {m.id[:8]} id does not match its stored fields")
        target = events_by_id.get(m.event_id)
        if target is None:
            issues.append(f"time_mark {m.id[:8]} references missing event {m.event_id[:8]}")
        elif m.recorded_at < target.recorded_at:
            issues.append(
                f"time_mark {m.id[:8]} recorded_at earlier than its event's recorded_at"
            )

    # seq unique and gapless from 1 across events, evidence and time_marks together.
    all_seqs = sorted(
        [e.seq for e in events] + [ev.seq for ev in evidence] + [m.seq for m in time_marks]
    )
    if all_seqs:
        expected = list(range(1, len(all_seqs) + 1))
        if all_seqs != expected:
            issues.append(
                "seq is not unique/gapless across events, evidence and time_marks"
            )

    # edge endpoints exist; parent seq/recorded_at ordering.
    for e in events:
        for edge in e.edges:
            if edge.label not in EDGE_LABELS:
                issues.append(f"event {e.id[:8]} has invalid edge label {edge.label!r}")
                continue
            target = events_by_id.get(edge.dst)
            if target is None:
                issues.append(
                    f"event {e.id[:8]} edge {edge.label}->{edge.dst[:8]} target missing"
                )
                continue
            if not (target.seq < e.seq):
                issues.append(
                    f"event {e.id[:8]} edge target {edge.dst[:8]} does not have lower seq"
                )
            if not (target.recorded_at <= e.recorded_at):
                issues.append(
                    f"event {e.id[:8]} edge target {edge.dst[:8]} has later recorded_at"
                )

    # evidence event_id/blob_hash referential checks.
    blob_rows = {r[0]: r for r in con.execute(
        "SELECT hash, media_type, size, recorded_at FROM blobs"
    ).fetchall()}
    for ev in evidence:
        target = events_by_id.get(ev.event_id)
        if target is None:
            issues.append(f"evidence {ev.id[:8]} references missing event {ev.event_id[:8]}")
        elif ev.recorded_at < target.recorded_at:
            issues.append(
                f"evidence {ev.id[:8]} recorded_at earlier than its event's recorded_at"
            )
        if ev.blob_hash not in blob_rows:
            issues.append(f"evidence {ev.id[:8]} references missing blob {ev.blob_hash[:8]}")

    # Re-evaluate historical append rules in each event's own past.
    issues.extend(_reverify_append_rules(events))

    blob_issues: list[str] = []
    if check_blobs:
        for hash_, (h, media_type, size, recorded_at) in blob_rows.items():
            if blob_store is None or not blob_store.exists(hash_):
                blob_issues.append(f"blob {hash_[:8]} unavailable")
                continue
            import hashlib

            hasher = hashlib.sha256()
            with blob_store.open(hash_) as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
            actual = hasher.hexdigest()
            if actual != hash_:
                blob_issues.append(f"blob {hash_[:8]} hash mismatch (tampered)")

    hard_failures = [i for i in blob_issues if "mismatch" in i]
    ok = not issues and not hard_failures
    return VerifyReport(ok=ok, issues=issues, blob_issues=blob_issues)


def _reverify_append_rules(events) -> list[str]:
    issues: list[str] = []
    events_sorted = sorted(events, key=lambda e: e.seq)
    events_by_id = {e.id: e for e in events_sorted}

    for e in events_sorted:
        target_ids = [ed.dst for ed in e.edges]
        past_ids: set[str] = set()
        stack = list(target_ids)
        while stack:
            cur = stack.pop()
            if cur in past_ids:
                continue
            past_ids.add(cur)
            t = events_by_id.get(cur)
            if t is None:
                continue
            for ed in t.edges:
                if ed.dst not in past_ids:
                    stack.append(ed.dst)
        past_events = [events_by_id[i] for i in past_ids if i in events_by_id]
        past_statuses = compute_status_all(past_events, [], ViewSelector())
        past_live = compute_live(past_events)

        fulfils_targets = {ed.dst for ed in e.edges if ed.label == "fulfils"}
        drops_targets = {ed.dst for ed in e.edges if ed.label == "drops"}
        if fulfils_targets & drops_targets:
            issues.append(
                f"event {e.id[:8]} both fulfils and drops {next(iter(fulfils_targets & drops_targets))[:8]}"
            )

        for edge in e.edges:
            target = events_by_id.get(edge.dst)
            if target is None:
                continue
            if edge.label == "needs":
                if target.kind not in OPENING_KINDS:
                    issues.append(
                        f"event {e.id[:8]} needs non-opening target {edge.dst[:8]}"
                    )
                elif not past_live.get(edge.dst, False):
                    issues.append(
                        f"event {e.id[:8]} needs non-live target {edge.dst[:8]} in its past"
                    )
                elif past_statuses.get(edge.dst) == "dropped":
                    issues.append(
                        f"event {e.id[:8]} needs dropped target {edge.dst[:8]} in its past"
                    )
            elif edge.label in ("fulfils", "drops"):
                if target.kind not in OPENING_KINDS:
                    issues.append(
                        f"event {e.id[:8]} {edge.label} non-opening target {edge.dst[:8]}"
                    )
                elif not past_live.get(edge.dst, False):
                    issues.append(
                        f"event {e.id[:8]} {edge.label} non-live target {edge.dst[:8]} in its past"
                    )
                else:
                    st = past_statuses.get(edge.dst)
                    if st not in ("ready", "blocked", "stuck"):
                        issues.append(
                            f"event {e.id[:8]} {edge.label} already-closed target {edge.dst[:8]} in its past"
                        )
            elif edge.label == "retracts":
                if not past_live.get(edge.dst, False):
                    issues.append(
                        f"event {e.id[:8]} retracts non-live target {edge.dst[:8]} in its past"
                    )
    return issues
