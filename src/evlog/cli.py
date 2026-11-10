"""Command-line interface for ev. Standard library argparse only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from . import blobs as blobs_mod
from . import interchange
from . import model
from . import reference as ref
from . import store as store_mod
from . import views as views_mod
from .model import (
    Event,
    EvidenceRow,
    ValidationError,
    format_timestamp,
    parse_any_timestamp,
    parse_strict_iso8601,
)
from .reference import (
    ALL_STATUSES,
    OPEN_STATUSES,
    STATUS_RETRACTED,
    ViewSelector,
    ancestors as ref_ancestors,
    apply_view,
    compute_live,
    compute_status_all,
)
from .store import (
    AmbiguousIdError,
    LedgerBusyError,
    NotFoundError,
    resolve_event_id,
)

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_USAGE = 2
EXIT_VERIFY_FAILED = 3
EXIT_BUSY = 4


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_REJECTED):
        super().__init__(message)
        self.code = code


# --- Formatting --------------------------------------------------------------


def short_id(full_id: str, full_ids: bool) -> str:
    return full_id if full_ids else full_id[:8]


def event_to_json(e: Event, status: str | None) -> dict:
    return {
        "id": e.id,
        "seq": e.seq,
        "kind": e.kind,
        "content": e.content,
        "actor": e.actor,
        "occurred_at": format_timestamp(e.occurred_at),
        "recorded_at": format_timestamp(e.recorded_at),
        "status": status,
        "edges": [{"label": ed.label, "dst": ed.dst} for ed in e.edges],
    }


def print_event_line(e: Event, status: str | None, *, json_mode: bool, full_ids: bool, out=None):
    if out is None:
        out = sys.stdout
    if json_mode:
        print(json.dumps(event_to_json(e, status), sort_keys=True), file=out)
    else:
        print(
            "\t".join(
                [
                    short_id(e.id, full_ids),
                    str(e.seq),
                    status if status is not None else "-",
                    e.kind,
                    e.content,
                ]
            ),
            file=out,
        )


def print_events(
    events: list[Event], statuses: dict[str, str], *, json_mode: bool, full_ids: bool, out=None
):
    if out is None:
        out = sys.stdout
    for e in events:
        print_event_line(e, statuses.get(e.id), json_mode=json_mode, full_ids=full_ids, out=out)


# --- Shared setup --------------------------------------------------------------


def get_connection(args) -> duckdb.DuckDBPyConnection:
    db_path = store_mod.resolve_db_path(args.db)
    if db_path is None:
        raise CliError(
            "no ledger found (looked for .ledger/ledger.duckdb in this "
            "directory and its parents). Run `ev init` here, or pass "
            "--db/EV_DB.",
            code=EXIT_REJECTED,
        )
    try:
        return store_mod.connect(db_path)
    except LedgerBusyError as e:
        raise CliError(str(e), code=EXIT_BUSY)


def build_selector_from_args(con, args) -> ViewSelector:
    return views_mod.build_selector(
        con, views_mod.ViewArgs(as_of=getattr(args, "as_of", None), line=getattr(args, "line", None))
    )


def resolve_or_raise(con, prefix: str) -> str:
    try:
        return resolve_event_id(con, prefix)
    except NotFoundError as e:
        raise CliError(str(e), code=EXIT_REJECTED)
    except AmbiguousIdError as e:
        raise CliError(str(e), code=EXIT_REJECTED)
    except ValidationError as e:
        raise CliError(str(e), code=EXIT_REJECTED)


def filter_evidence_rows(rows: list[dict], selector: ViewSelector) -> list[dict]:
    """rows come from blobs_mod.list_evidence_for_event: `recorded_at` is
    a raw value from DuckDB (a datetime) and `seq` an int."""
    out = []
    for r in rows:
        if selector.as_of_seq is not None and r["seq"] > selector.as_of_seq:
            continue
        if selector.as_of_time is not None:
            rt = parse_any_timestamp(r["recorded_at"])
            if rt > selector.as_of_time:
                continue
        out.append(r)
    return out


def evidence_row_for_json(r: dict) -> dict:
    rr = dict(r)
    rr["recorded_at"] = format_timestamp(parse_any_timestamp(rr["recorded_at"]))
    return rr


# --- Argument parser -----------------------------------------------------------


def add_global_flags(p: argparse.ArgumentParser):
    p.add_argument("--db", default=None, help="path to ledger.duckdb")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--full-ids", action="store_true", help="print full ids")


def add_view_flags(p: argparse.ArgumentParser):
    p.add_argument("--as-of", default=None, help="seq (int) or ISO timestamp")
    p.add_argument("--line", default=None, help="restrict to ancestors of this id, plus itself")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ev")
    add_global_flags(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init")
    add_global_flags(p)

    p = sub.add_parser("add")
    add_global_flags(p)
    p.add_argument("kind")
    p.add_argument("content")
    p.add_argument("--from", dest="from_", action="append", default=[])
    p.add_argument("--needs", action="append", default=[])
    p.add_argument("--fulfils", action="append", default=[])
    p.add_argument("--drops", action="append", default=[])
    p.add_argument("--retracts", action="append", default=[])
    p.add_argument("--at", default=None)
    p.add_argument("--actor", default=None)
    p.add_argument("--evidence", action="append", default=[])

    p = sub.add_parser("do")
    add_global_flags(p)
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--actor", default=None)
    p.add_argument("--at", default=None)

    p = sub.add_parser("answer")
    add_global_flags(p)
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--actor", default=None)
    p.add_argument("--at", default=None)

    p = sub.add_parser("drop")
    add_global_flags(p)
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--actor", default=None)
    p.add_argument("--at", default=None)

    p = sub.add_parser("retract")
    add_global_flags(p)
    p.add_argument("id")
    p.add_argument("text")
    p.add_argument("--descendants", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--actor", default=None)
    p.add_argument("--at", default=None)

    p = sub.add_parser("supersede")
    add_global_flags(p)
    p.add_argument("id")
    p.add_argument("kind")
    p.add_argument("text")
    p.add_argument("--from", dest="from_", action="append", default=[])
    p.add_argument("--needs", action="append", default=[])
    p.add_argument("--fulfils", action="append", default=[])
    p.add_argument("--retracts", action="append", default=[])
    p.add_argument("--actor", default=None)
    p.add_argument("--at", default=None)

    p = sub.add_parser("show")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("id")

    p = sub.add_parser("status")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("id")

    p = sub.add_parser("log")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("--kind", default=None)
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("ls")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("--status", action="append", default=[])
    p.add_argument("--kind", default=None)
    p.add_argument("--include-retracted", action="store_true")
    p.add_argument("--overdue", action="store_true", help="only events past their current time mark")

    p = sub.add_parser("find")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("query")
    p.add_argument("--status", action="append", default=[])
    p.add_argument("--kind", default=None)
    p.add_argument("--include-retracted", action="store_true")

    p = sub.add_parser("why")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("id")

    p = sub.add_parser("then")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("id")

    p = sub.add_parser("tips")
    add_global_flags(p)
    add_view_flags(p)

    p = sub.add_parser("diff")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("a")
    p.add_argument("b")

    p = sub.add_parser("verify")
    add_global_flags(p)
    p.add_argument("--blobs", action="store_true")

    p = sub.add_parser("sql")
    add_global_flags(p)
    p.add_argument("query", help="read-only SQL; use '-' to read from stdin")

    p = sub.add_parser("export")
    add_global_flags(p)
    p.add_argument("file", nargs="?", default=None)

    p = sub.add_parser("import")
    add_global_flags(p)
    p.add_argument("file")

    p = sub.add_parser("attach")
    add_global_flags(p)
    p.add_argument("event")
    p.add_argument("file")
    p.add_argument("--note", default="")

    p = sub.add_parser("evidence")
    add_global_flags(p)
    p.add_argument("event")
    p.add_argument("--as-of", default=None)

    p = sub.add_parser("when")
    add_global_flags(p)
    p.add_argument("event")
    p.add_argument("time", help="ISO 8601 with explicit offset or Z; may be future or past")
    p.add_argument("--note", default="")
    p.add_argument("--actor", default=None)

    p = sub.add_parser("schedule")
    add_global_flags(p)
    add_view_flags(p)
    p.add_argument("--from-time", dest="from_time", default=None, help="ISO 8601, inclusive")
    p.add_argument("--to-time", dest="to_time", default=None, help="ISO 8601, inclusive")
    p.add_argument("--kind", default=None)
    p.add_argument("--status", action="append", default=[])
    p.add_argument("--overdue", action="store_true")
    p.add_argument("--include-retracted", action="store_true")

    p = sub.add_parser("blob")
    add_global_flags(p)
    blob_sub = p.add_subparsers(dest="blob_command", required=True)
    g = blob_sub.add_parser("get")
    g.add_argument("hash")
    g.add_argument("--out", required=True)
    r = blob_sub.add_parser("relocate")
    r.add_argument("hash")
    r.add_argument("uri")

    return parser


# --- Command handlers ------------------------------------------------------


def cmd_init(args) -> int:
    path = store_mod.init_ledger(Path.cwd())
    if args.json:
        print(json.dumps({"db": str(path)}))
    else:
        print(f"initialised ledger at {path}")
    return EXIT_OK


def _edge_args_to_pairs(args) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for x in getattr(args, "from_", []) or []:
        edges.append(("from", x))
    for x in getattr(args, "needs", []) or []:
        edges.append(("needs", x))
    for x in getattr(args, "fulfils", []) or []:
        edges.append(("fulfils", x))
    for x in getattr(args, "drops", []) or []:
        edges.append(("drops", x))
    for x in getattr(args, "retracts", []) or []:
        edges.append(("retracts", x))
    return edges


def _read_content(content: str) -> str:
    if content == "-":
        return sys.stdin.read()
    return content


def _do_add(con, args, kind, content, edge_pairs, evidence_files: list[str]):
    content = _read_content(content)
    if evidence_files:
        con.execute("BEGIN TRANSACTION")
        try:
            result = store_mod.append_no_txn(
                con,
                kind=kind,
                content=content,
                edges=edge_pairs,
                actor=args.actor,
                at=args.at,
            )
            if result.created:
                blob_store = blobs_mod.blob_store_for_db(
                    store_mod.resolve_db_path(args.db)
                )
                for f in evidence_files:
                    blobs_mod.attach_file(con, blob_store, result.id, Path(f))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    else:
        result = store_mod.append(
            con,
            kind=kind,
            content=content,
            edges=edge_pairs,
            actor=args.actor,
            at=args.at,
        )
    return result


def cmd_add(args) -> int:
    con = get_connection(args)
    try:
        edge_pairs = _edge_args_to_pairs(args)
        try:
            result = _do_add(con, args, args.kind, args.content, edge_pairs, args.evidence)
        except ValidationError as e:
            raise CliError(str(e))
        for w in result.warnings:
            print(f"warning: {w}", file=sys.stderr)
        if args.json:
            print(json.dumps({"id": result.id}))
        else:
            print(result.id if args.full_ids else short_id(result.id, False))
        return EXIT_OK
    finally:
        con.close()


def _sugar_add(args, kind: str, edge_label: str) -> int:
    con = get_connection(args)
    try:
        target = resolve_or_raise(con, args.id)
        edge_pairs = [(edge_label, target)]
        try:
            result = store_mod.append(
                con,
                kind=kind,
                content=args.text,
                edges=edge_pairs,
                actor=args.actor,
                at=args.at,
            )
        except ValidationError as e:
            raise CliError(str(e))
        for w in result.warnings:
            print(f"warning: {w}", file=sys.stderr)
        if args.json:
            print(json.dumps({"id": result.id}))
        else:
            print(result.id if args.full_ids else short_id(result.id, False))
        return EXIT_OK
    finally:
        con.close()


def cmd_do(args) -> int:
    return _sugar_add(args, "do", "fulfils")


def cmd_answer(args) -> int:
    return _sugar_add(args, "answer", "fulfils")


def cmd_drop(args) -> int:
    return _sugar_add(args, "drop", "drops")


def cmd_retract(args) -> int:
    con = get_connection(args)
    try:
        target = resolve_or_raise(con, args.id)
        ids_to_retract = [target]
        if args.descendants:
            events, evidence = views_mod.load_ledger(con)
            live = compute_live(events)
            seq_by_id = {e.id: e.seq for e in events}
            desc_ids = views_mod.descendants_of(con, target)
            extra = sorted(
                (d for d in desc_ids if live.get(d, False)),
                key=lambda d: seq_by_id.get(d, 0),
            )
            ids_to_retract.extend(extra)

        if args.dry_run:
            if args.json:
                print(json.dumps({"would_retract": [i if args.full_ids else short_id(i, False) for i in ids_to_retract]}))
            else:
                for i in ids_to_retract:
                    print(i if args.full_ids else short_id(i, False))
            return EXIT_OK

        con.execute("BEGIN TRANSACTION")
        try:
            new_ids = []
            first = True
            for tid in ids_to_retract:
                text = args.text if first else f"{args.text} (cascade from {short_id(target, False)})"
                first = False
                result = store_mod.append_no_txn(
                    con,
                    kind="retract",
                    content=text,
                    edges=[("retracts", tid)],
                    actor=args.actor,
                    at=args.at,
                )
                new_ids.append(result.id)
            con.execute("COMMIT")
        except ValidationError as e:
            con.execute("ROLLBACK")
            raise CliError(str(e))
        except Exception:
            con.execute("ROLLBACK")
            raise

        if args.json:
            print(json.dumps({"id": new_ids[0], "ids": new_ids}))
        else:
            print(new_ids[0] if args.full_ids else short_id(new_ids[0], False))
        return EXIT_OK
    finally:
        con.close()


def cmd_supersede(args) -> int:
    con = get_connection(args)
    try:
        target = resolve_or_raise(con, args.id)
        edge_pairs = _edge_args_to_pairs(args)
        edge_pairs.append(("drops", target))
        try:
            result = store_mod.append(
                con,
                kind=args.kind,
                content=args.text,
                edges=edge_pairs,
                actor=args.actor,
                at=args.at,
            )
        except ValidationError as e:
            raise CliError(str(e))
        for w in result.warnings:
            print(f"warning: {w}", file=sys.stderr)
        if args.json:
            print(json.dumps({"id": result.id}))
        else:
            print(result.id if args.full_ids else short_id(result.id, False))
        return EXIT_OK
    finally:
        con.close()


def cmd_show(args) -> int:
    con = get_connection(args)
    try:
        eid = resolve_or_raise(con, args.id)
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        events_by_id = {e.id: e for e in events}
        e = events_by_id.get(eid)
        if e is None:
            raise CliError(f"event {args.id} not found")

        view_events, view_evidence = apply_view(events, evidence, selector)
        view_ids = {v.id for v in view_events}
        if eid not in view_ids:
            raise CliError(f"event {args.id} not visible in this view")

        statuses = compute_status_all(events, evidence, selector)
        status = statuses.get(eid)

        incoming = [
            (other.id, ed.label)
            for other in view_events
            for ed in other.edges
            if ed.dst == eid
        ]

        needs_status = []
        for ed in e.edges:
            if ed.label == "needs":
                needs_status.append((ed.dst, statuses.get(ed.dst)))

        ev_rows = filter_evidence_rows(
            blobs_mod.list_evidence_for_event(con, eid), selector
        )

        current_marks = views_mod.current_time_marks_in_view(con, selector)
        current_mark = current_marks.get(eid)
        now = model.utcnow()
        overdue = ref.is_overdue(status, current_mark, now)

        if args.json:
            obj = event_to_json(e, status)
            obj["incoming_edges"] = [{"src": s, "label": l} for s, l in incoming]
            obj["needs_status"] = [{"id": i, "status": st} for i, st in needs_status]
            obj["evidence"] = [evidence_row_for_json(r) for r in ev_rows]
            obj["when"] = format_timestamp(current_mark.at) if current_mark else None
            obj["overdue"] = overdue
            print(json.dumps(obj, sort_keys=True))
        else:
            print_event_line(e, status, json_mode=False, full_ids=args.full_ids)
            print(f"  occurred_at: {format_timestamp(e.occurred_at)}")
            print(f"  recorded_at: {format_timestamp(e.recorded_at)}")
            print(f"  actor: {e.actor}")
            if current_mark is not None:
                flag = "  (OVERDUE)" if overdue else ""
                print(f"  when: {format_timestamp(current_mark.at)}{flag}")
            print("  outgoing edges:")
            for ed in e.edges:
                print(f"    {ed.label} -> {short_id(ed.dst, args.full_ids)}")
            print("  incoming edges:")
            for s, l in incoming:
                print(f"    {l} <- {short_id(s, args.full_ids)}")
            if needs_status:
                print("  needs targets:")
                for i, st in needs_status:
                    print(f"    {short_id(i, args.full_ids)}: {st}")
            if ev_rows:
                print("  evidence:")
                for r in ev_rows:
                    print(f"    {r['blob_hash'][:8]} {r['note']}")
        return EXIT_OK
    finally:
        con.close()


def cmd_status(args) -> int:
    con = get_connection(args)
    try:
        eid = resolve_or_raise(con, args.id)
        selector = build_selector_from_args(con, args)
        st = views_mod.status_of_id(con, eid, selector)
        if st is None:
            raise CliError(f"event {args.id} not visible in this view")
        if args.json:
            print(json.dumps({"id": eid, "status": st}))
        else:
            print(st)
        return EXIT_OK
    finally:
        con.close()


def cmd_log(args) -> int:
    con = get_connection(args)
    try:
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        from .reference import apply_view, compute_status_all

        view_events, _ = apply_view(events, evidence, selector)
        if args.kind:
            view_events = [e for e in view_events if e.kind == args.kind]
        if args.limit:
            view_events = view_events[-args.limit:]
        statuses = compute_status_all(events, evidence, selector)
        print_events(view_events, statuses, json_mode=args.json, full_ids=args.full_ids)
        return EXIT_OK
    finally:
        con.close()


def cmd_ls(args) -> int:
    con = get_connection(args)
    try:
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        from .reference import apply_view, compute_status_all, STATUS_RETRACTED

        view_events, _ = apply_view(events, evidence, selector)
        statuses = compute_status_all(events, evidence, selector)
        current_marks = (
            views_mod.current_time_marks_in_view(con, selector) if args.overdue else {}
        )
        now = model.utcnow()

        wanted_statuses = args.status or ["open"]
        for s in wanted_statuses:
            if s not in ALL_STATUSES:
                raise CliError(f"unknown status {s!r}", code=EXIT_USAGE)

        def matches(e) -> bool:
            st = statuses.get(e.id)
            if st == STATUS_RETRACTED and not args.include_retracted:
                return False
            if args.kind and e.kind != args.kind:
                return False
            if args.overdue and not ref.is_overdue(st, current_marks.get(e.id), now):
                return False
            for want in wanted_statuses:
                if want == "open" and st in OPEN_STATUSES:
                    return True
                if want == st:
                    return True
            return False

        filtered = [e for e in view_events if matches(e)]
        print_events(filtered, statuses, json_mode=args.json, full_ids=args.full_ids)
        return EXIT_OK
    finally:
        con.close()


def cmd_find(args) -> int:
    """Keyword search over event content. This is a simple
    case-insensitive substring match today; it exists so that finding
    "the event the user probably means" is a single reliable command
    instead of every caller hand-filtering `ev ls`/`ev log --json`
    output. If/when this grows a semantic/embedding-backed mode (e.g.
    via DuckDB's vector-similarity-search extension), it should stay
    behind this same `ev find` interface rather than becoming a
    separate command — see `ev sql` below for ad-hoc querying in the
    meantime."""
    con = get_connection(args)
    try:
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        from .reference import apply_view, compute_status_all, STATUS_RETRACTED

        view_events, _ = apply_view(events, evidence, selector)
        statuses = compute_status_all(events, evidence, selector)

        wanted_statuses = args.status  # no default: search isn't status-scoped
        for s in wanted_statuses:
            if s not in ALL_STATUSES:
                raise CliError(f"unknown status {s!r}", code=EXIT_USAGE)

        needle = args.query.lower()

        def matches(e) -> bool:
            if needle not in e.content.lower():
                return False
            st = statuses.get(e.id)
            if st == STATUS_RETRACTED and not args.include_retracted:
                return False
            if args.kind and e.kind != args.kind:
                return False
            if wanted_statuses:
                for want in wanted_statuses:
                    if want == "open" and st in OPEN_STATUSES:
                        return True
                    if want == st:
                        return True
                return False
            return True

        filtered = [e for e in view_events if matches(e)]
        print_events(filtered, statuses, json_mode=args.json, full_ids=args.full_ids)
        return EXIT_OK
    finally:
        con.close()


def cmd_why(args) -> int:
    con = get_connection(args)
    try:
        eid = resolve_or_raise(con, args.id)
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        from .reference import apply_view, ancestors, compute_status_all

        view_events, _ = apply_view(events, evidence, selector)
        view_ids = {e.id for e in view_events}
        events_by_id = {e.id: e for e in view_events}
        anc_ids = ancestors(eid, events_by_id) if eid in view_ids else set()
        statuses = compute_status_all(events, evidence, selector)

        depth_map = _depth_map(eid, events_by_id)
        rows = sorted(
            [events_by_id[i] for i in anc_ids if i in events_by_id],
            key=lambda e: depth_map.get(e.id, 0),
        )
        _print_with_depth(rows, depth_map, statuses, args)
        return EXIT_OK
    finally:
        con.close()


def cmd_then(args) -> int:
    con = get_connection(args)
    try:
        eid = resolve_or_raise(con, args.id)
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        from .reference import apply_view, compute_status_all

        view_events, _ = apply_view(events, evidence, selector)
        view_ids = {e.id for e in view_events}
        events_by_id = {e.id: e for e in view_events}

        desc_ids = views_mod.descendants_of(con, eid) & view_ids
        depth_map = _depth_map_descendants(eid, events_by_id)
        statuses = compute_status_all(events, evidence, selector)
        rows = sorted(
            [events_by_id[i] for i in desc_ids if i in events_by_id],
            key=lambda e: depth_map.get(e.id, 0),
        )
        _print_with_depth(rows, depth_map, statuses, args)
        return EXIT_OK
    finally:
        con.close()


def _depth_map(start_id: str, events_by_id: dict) -> dict:
    depth = {}
    queue = [(start_id, 0)]
    seen = set()
    while queue:
        cur, d = queue.pop(0)
        ev = events_by_id.get(cur)
        if ev is None:
            continue
        for ed in ev.edges:
            if ed.dst not in seen or depth.get(ed.dst, 10**9) > d + 1:
                seen.add(ed.dst)
                depth[ed.dst] = min(depth.get(ed.dst, 10**9), d + 1)
                queue.append((ed.dst, d + 1))
    return depth


def _depth_map_descendants(start_id: str, events_by_id: dict) -> dict:
    children: dict[str, list[str]] = {}
    for e in events_by_id.values():
        for ed in e.edges:
            children.setdefault(ed.dst, []).append(e.id)
    depth = {}
    queue = [(start_id, 0)]
    seen = set()
    while queue:
        cur, d = queue.pop(0)
        for child in children.get(cur, []):
            if child not in seen or depth.get(child, 10**9) > d + 1:
                seen.add(child)
                depth[child] = min(depth.get(child, 10**9), d + 1)
                queue.append((child, d + 1))
    return depth


def _print_with_depth(rows, depth_map, statuses, args):
    for e in rows:
        d = depth_map.get(e.id, 0)
        st = statuses.get(e.id)
        if args.json:
            obj = event_to_json(e, st)
            obj["depth"] = d
            print(json.dumps(obj, sort_keys=True))
        else:
            print(
                "\t".join(
                    [
                        str(d),
                        short_id(e.id, args.full_ids),
                        str(e.seq),
                        st if st is not None else "-",
                        e.kind,
                        e.content,
                    ]
                )
            )


def cmd_tips(args) -> int:
    con = get_connection(args)
    try:
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        from .reference import compute_status_all

        tip_ids = views_mod.list_tips(con, selector)
        statuses = compute_status_all(events, evidence, selector)
        events_by_id = {e.id: e for e in events}
        rows = [events_by_id[i] for i in tip_ids if i in events_by_id]
        rows.sort(key=lambda e: e.seq)
        print_events(rows, statuses, json_mode=args.json, full_ids=args.full_ids)
        return EXIT_OK
    finally:
        con.close()


def cmd_diff(args) -> int:
    con = get_connection(args)
    try:
        a = resolve_or_raise(con, args.a)
        b = resolve_or_raise(con, args.b)
        only_a, only_b = views_mod.diff(con, a, b)
        if args.json:
            print(
                json.dumps(
                    {
                        "only_a": sorted(short_id(i, args.full_ids) for i in only_a),
                        "only_b": sorted(short_id(i, args.full_ids) for i in only_b),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"only in {short_id(a, args.full_ids)}:")
            for i in sorted(only_a):
                print(f"  {short_id(i, args.full_ids)}")
            print(f"only in {short_id(b, args.full_ids)}:")
            for i in sorted(only_b):
                print(f"  {short_id(i, args.full_ids)}")
        return EXIT_OK
    finally:
        con.close()


def cmd_verify(args) -> int:
    con = get_connection(args)
    try:
        blob_store = None
        if args.blobs:
            blob_store = blobs_mod.blob_store_for_db(store_mod.resolve_db_path(args.db))
        report = interchange.verify(con, check_blobs=args.blobs, blob_store=blob_store)
        if args.json:
            print(
                json.dumps(
                    {"ok": report.ok, "issues": report.issues, "blob_issues": report.blob_issues}
                )
            )
        else:
            if report.ok:
                print("ok")
            for i in report.issues:
                print(f"FAIL: {i}")
            for i in report.blob_issues:
                print(f"blob: {i}")
        return EXIT_OK if report.ok else EXIT_VERIFY_FAILED
    finally:
        con.close()


def cmd_sql(args) -> int:
    """Run arbitrary read-only SQL against the ledger. This is an
    escape hatch, not a replacement for the other commands: DuckDB
    itself rejects any write statement on this connection (we open it
    with read_only=True), so there's no need to sandbox the query text
    ourselves and no risk to the append-only guarantee. Use this for
    ad-hoc exploration, joins across events/edges/evidence/blobs that
    `ev find`/`ev ls`/`ev log` don't cover, or future extensions (e.g.
    DuckDB's vector-similarity-search extension over an embeddings
    table) without needing a new CLI flag for every use case.

    Schema: events(seq, id, kind, content, actor, occurred_at,
    recorded_at), edges(src, dst, label), evidence(seq, id, event_id,
    blob_hash, note, actor, recorded_at), blobs(hash, media_type,
    size, recorded_at), blob_locations(hash, uri, recorded_at).
    Status is NOT a column anywhere — it's derived (see `ev show`/`ev
    status`/`reference.py`), so raw SQL can see the graph but not the
    computed status directly."""
    query = args.query
    if query == "-":
        query = sys.stdin.read()

    db_path = store_mod.resolve_db_path(args.db)
    if db_path is None:
        raise CliError(
            "no ledger found (looked for .ledger/ledger.duckdb in this "
            "directory and its parents). Run `ev init` here, or pass "
            "--db/EV_DB.",
            code=EXIT_REJECTED,
        )
    try:
        con = store_mod.connect_read_only(db_path)
    except store_mod.NotFoundError as e:
        raise CliError(str(e))
    except store_mod.LedgerBusyError as e:
        raise CliError(str(e), code=EXIT_BUSY)

    try:
        try:
            result = con.execute(query)
        except duckdb.Error as e:
            raise CliError(f"sql error: {e}")
        cols = [d[0] for d in result.description] if result.description else []
        rows = result.fetchall()
    finally:
        con.close()

    if args.json:
        for row in rows:
            print(json.dumps(dict(zip(cols, row)), default=str, sort_keys=True))
    else:
        if cols:
            print("\t".join(cols))
        for row in rows:
            print("\t".join("" if v is None else str(v) for v in row))
    return EXIT_OK


def cmd_export(args) -> int:
    con = get_connection(args)
    try:
        path = Path(args.file) if args.file else None
        text = interchange.export_to(con, path)
        if path is None:
            sys.stdout.write(text)
        return EXIT_OK
    finally:
        con.close()


def cmd_import(args) -> int:
    con = get_connection(args)
    try:
        try:
            stats = interchange.import_from(con, Path(args.file))
        except ValidationError as e:
            raise CliError(str(e))
        if args.json:
            print(
                json.dumps(
                    {
                        "blobs_added": stats.blobs_added,
                        "locations_added": stats.locations_added,
                        "events_added": stats.events_added,
                        "evidence_added": stats.evidence_added,
                        "time_marks_added": stats.time_marks_added,
                        "skipped": stats.skipped,
                    }
                )
            )
        else:
            print(
                f"events +{stats.events_added} evidence +{stats.evidence_added} "
                f"time_marks +{stats.time_marks_added} "
                f"blobs +{stats.blobs_added} locations +{stats.locations_added} "
                f"skipped {stats.skipped}"
            )
        return EXIT_OK
    finally:
        con.close()


def cmd_attach(args) -> int:
    con = get_connection(args)
    try:
        eid = resolve_or_raise(con, args.event)
        blob_store = blobs_mod.blob_store_for_db(store_mod.resolve_db_path(args.db))
        con.execute("BEGIN TRANSACTION")
        try:
            blob_hash, evidence_id = blobs_mod.attach_file(
                con, blob_store, eid, Path(args.file), note=args.note
            )
            con.execute("COMMIT")
        except ValidationError as e:
            con.execute("ROLLBACK")
            raise CliError(str(e))
        except Exception:
            con.execute("ROLLBACK")
            raise
        if args.json:
            print(json.dumps({"blob_hash": blob_hash, "evidence_id": evidence_id}))
        else:
            print(f"{blob_hash[:8]}\t{evidence_id[:8] if not args.full_ids else evidence_id}")
        return EXIT_OK
    finally:
        con.close()


def cmd_evidence(args) -> int:
    con = get_connection(args)
    try:
        eid = resolve_or_raise(con, args.event)
        rows = blobs_mod.list_evidence_for_event(con, eid)
        if args.as_of is not None:
            s = args.as_of
            if s.lstrip("-").isdigit():
                selector = ViewSelector(as_of_seq=int(s))
            else:
                selector = ViewSelector(as_of_time=parse_strict_iso8601(s))
            rows = filter_evidence_rows(rows, selector)
        if args.json:
            for r in rows:
                print(json.dumps(evidence_row_for_json(r), sort_keys=True))
        else:
            for r in rows:
                print(f"{r['blob_hash'][:8]}\t{r['seq']}\t{r['note']}\t{r['location']}")
        return EXIT_OK
    finally:
        con.close()


def cmd_when(args) -> int:
    """Set/correct the single instant an event is or was scheduled
    for. Unconstrained: unlike `--at` on `ev add`, this may be in the
    future (that's the point — plan ahead) or used to correct a
    mistaken past instant without touching the event itself."""
    con = get_connection(args)
    try:
        eid = resolve_or_raise(con, args.event)
        try:
            result = store_mod.mark_time(
                con, event_id=eid, at=args.time, note=args.note, actor=args.actor
            )
        except ValidationError as e:
            raise CliError(str(e))
        if args.json:
            print(json.dumps({"id": result.id, "event_id": eid}))
        else:
            print(result.id if args.full_ids else short_id(result.id, False))
        return EXIT_OK
    finally:
        con.close()


def cmd_schedule(args) -> int:
    """List events by their effective time (current time mark, else
    `occurred_at`) — "what's coming up", "what happened when", and
    (with --overdue) "what's late". This is the time-axis complement
    to `ev log` (seq order) and `ev ls` (status filter)."""
    con = get_connection(args)
    try:
        selector = build_selector_from_args(con, args)
        events, evidence = views_mod.load_ledger(con)
        view_events, _ = apply_view(events, evidence, selector)
        statuses = compute_status_all(events, evidence, selector)
        current_marks = views_mod.current_time_marks_in_view(con, selector)
        now = model.utcnow()

        wanted_statuses = args.status
        for s in wanted_statuses:
            if s not in ALL_STATUSES:
                raise CliError(f"unknown status {s!r}", code=EXIT_USAGE)

        from_time = parse_strict_iso8601(args.from_time) if args.from_time else None
        to_time = parse_strict_iso8601(args.to_time) if args.to_time else None

        rows = []
        for e in view_events:
            st = statuses.get(e.id)
            if st == STATUS_RETRACTED and not args.include_retracted:
                continue
            if args.kind and e.kind != args.kind:
                continue
            mark = current_marks.get(e.id)
            overdue = ref.is_overdue(st, mark, now)
            if args.overdue and not overdue:
                continue
            if wanted_statuses:
                matched = False
                for want in wanted_statuses:
                    if want == "open" and st in OPEN_STATUSES:
                        matched = True
                    elif want == st:
                        matched = True
                if not matched:
                    continue
            when = ref.effective_time(e, mark)
            if from_time is not None and when < from_time:
                continue
            if to_time is not None and when > to_time:
                continue
            rows.append((when, e, st, mark, overdue))

        rows.sort(key=lambda r: r[0])

        for when, e, st, mark, overdue in rows:
            if args.json:
                obj = event_to_json(e, st)
                obj["when"] = format_timestamp(when)
                obj["when_is_mark"] = mark is not None
                obj["overdue"] = overdue
                print(json.dumps(obj, sort_keys=True))
            else:
                flag = " OVERDUE" if overdue else ""
                print(
                    "\t".join(
                        [
                            format_timestamp(when) + flag,
                            short_id(e.id, args.full_ids),
                            st if st is not None else "-",
                            e.kind,
                            e.content,
                        ]
                    )
                )
        return EXIT_OK
    finally:
        con.close()


def cmd_blob(args) -> int:
    con = get_connection(args)
    try:
        blob_store = blobs_mod.blob_store_for_db(store_mod.resolve_db_path(args.db))
        if args.blob_command == "get":
            if not blob_store.exists(args.hash):
                raise CliError(f"blob {args.hash[:8]} unavailable")
            with blob_store.open(args.hash) as src, open(args.out, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
            return EXIT_OK
        elif args.blob_command == "relocate":
            try:
                blobs_mod.relocate_blob(con, args.hash, args.uri)
            except ValidationError as e:
                raise CliError(str(e))
            return EXIT_OK
        else:
            raise CliError("unknown blob subcommand", code=EXIT_USAGE)
    finally:
        con.close()


COMMANDS = {
    "init": cmd_init,
    "add": cmd_add,
    "do": cmd_do,
    "answer": cmd_answer,
    "drop": cmd_drop,
    "retract": cmd_retract,
    "supersede": cmd_supersede,
    "show": cmd_show,
    "status": cmd_status,
    "log": cmd_log,
    "ls": cmd_ls,
    "find": cmd_find,
    "why": cmd_why,
    "then": cmd_then,
    "tips": cmd_tips,
    "diff": cmd_diff,
    "verify": cmd_verify,
    "sql": cmd_sql,
    "export": cmd_export,
    "import": cmd_import,
    "attach": cmd_attach,
    "evidence": cmd_evidence,
    "when": cmd_when,
    "schedule": cmd_schedule,
    "blob": cmd_blob,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else EXIT_USAGE
        return code if code == 0 else EXIT_USAGE

    handler = COMMANDS.get(args.command)
    if handler is None:
        print(f"unknown command {args.command!r}", file=sys.stderr)
        return EXIT_USAGE

    try:
        return handler(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except LedgerBusyError as e:
        print(f"error: ledger busy: {e}", file=sys.stderr)
        return EXIT_BUSY
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_REJECTED
    except (NotFoundError, AmbiguousIdError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_REJECTED


if __name__ == "__main__":
    sys.exit(main())
