"""Test helpers that drive the real CLI, per section 10.2 of the spec."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

from evlog.cli import main as cli_main


def run_cli(args: list[str], cwd: Path):
    old_cwd = Path.cwd()
    os.chdir(cwd)
    try:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = cli_main(args)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()
    finally:
        os.chdir(old_cwd)


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        code, out, err = self.run(["init"])
        assert code == 0, err

    def run(self, args: list[str]):
        return run_cli(args, self.path)

    def _view_flags(self, view: dict) -> list[str]:
        flags = []
        if "as_of" in view and view["as_of"] is not None:
            flags += ["--as-of", str(view["as_of"])]
        if "line" in view and view["line"] is not None:
            flags += ["--line", view["line"]]
        return flags

    def add(self, kind: str, text: str, at=None, actor=None, **edges) -> str:
        args = ["add", "--json", kind, text]
        for label in ("from", "needs", "fulfils", "drops", "retracts"):
            vals = edges.get(label, [])
            flag = "--from" if label == "from" else f"--{label}"
            for v in vals:
                args += [flag, v]
        if at is not None:
            args += ["--at", at]
        if actor is not None:
            args += ["--actor", actor]
        code, out, err = self.run(args)
        if code != 0:
            raise AssertionError(f"add failed (exit {code}): {err}")
        return json.loads(out.strip().splitlines()[-1])["id"]

    def do(self, id_: str, text: str, **kw) -> str:
        return self._sugar("do", id_, text, **kw)

    def answer(self, id_: str, text: str, **kw) -> str:
        return self._sugar("answer", id_, text, **kw)

    def drop(self, id_: str, text: str, **kw) -> str:
        return self._sugar("drop", id_, text, **kw)

    def _sugar(self, cmd: str, id_: str, text: str, actor=None, at=None) -> str:
        args = [cmd, "--json", id_, text]
        if actor is not None:
            args += ["--actor", actor]
        if at is not None:
            args += ["--at", at]
        code, out, err = self.run(args)
        if code != 0:
            raise AssertionError(f"{cmd} failed (exit {code}): {err}")
        return json.loads(out.strip().splitlines()[-1])["id"]

    def retract(self, id_: str, text: str, descendants=False, dry_run=False, actor=None, at=None):
        args = ["retract", "--json", id_, text]
        if descendants:
            args.append("--descendants")
        if dry_run:
            args.append("--dry-run")
        if actor is not None:
            args += ["--actor", actor]
        if at is not None:
            args += ["--at", at]
        code, out, err = self.run(args)
        if code != 0:
            raise AssertionError(f"retract failed (exit {code}): {err}")
        return json.loads(out.strip().splitlines()[-1])

    def supersede(self, id_: str, kind: str, text: str, **edges) -> str:
        args = ["supersede", "--json", id_, kind, text]
        for label in ("from", "needs", "fulfils", "retracts"):
            vals = edges.get(label, [])
            flag = "--from" if label == "from" else f"--{label}"
            for v in vals:
                args += [flag, v]
        code, out, err = self.run(args)
        if code != 0:
            raise AssertionError(f"supersede failed (exit {code}): {err}")
        return json.loads(out.strip().splitlines()[-1])["id"]

    def status(self, id_: str, **view):
        args = ["status", "--json", id_] + self._view_flags(view)
        code, out, err = self.run(args)
        if code != 0:
            return None
        return json.loads(out.strip().splitlines()[-1])["status"]

    def seq(self, id_: str) -> int:
        code, out, err = self.run(["show", "--json", id_])
        assert code == 0, err
        return json.loads(out.strip().splitlines()[-1])["seq"]

    def when(self, event_id: str, time: str, note: str = "", actor=None):
        args = ["when", "--json", event_id, time]
        if note:
            args += ["--note", note]
        if actor is not None:
            args += ["--actor", actor]
        code, out, err = self.run(args)
        if code != 0:
            raise AssertionError(f"when failed (exit {code}): {err}")
        return json.loads(out.strip().splitlines()[-1])["id"]

    def show(self, event_id: str, **view) -> dict:
        args = ["show", "--json", event_id] + self._view_flags(view)
        code, out, err = self.run(args)
        assert code == 0, err
        return json.loads(out.strip().splitlines()[-1])

    def schedule(self, *cmdargs, **view) -> list:
        args = ["schedule"] + list(cmdargs) + ["--json"] + self._view_flags(view)
        code, out, err = self.run(args)
        assert code == 0, err
        return [json.loads(l) for l in out.splitlines() if l.strip()]

    def ids(self, *cmdargs, **view) -> set:
        args = list(cmdargs) + ["--json"] + self._view_flags(view)
        code, out, err = self.run(args)
        if code != 0:
            return set()
        result = set()
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "id" in obj:
                result.add(obj["id"])
        return result

    def diff(self, a: str, b: str):
        code, out, err = self.run(["diff", "--json", "--full-ids", a, b])
        assert code == 0, err
        obj = json.loads(out.strip().splitlines()[-1])
        return set(obj["only_a"]), set(obj["only_b"])

    def all_statuses(self, **view) -> dict:
        args = ["log", "--json"] + self._view_flags(view)
        code, out, err = self.run(args)
        assert code == 0, err
        result = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            result[obj["id"]] = obj["status"]
        return result

    def attach(self, event_id: str, data: bytes, note: str = "", filename: str = "evidence.bin") -> str:
        f = self.path / filename
        # Avoid clobbering identical-name files with different content across
        # calls within one test by hashing into the name.
        import hashlib

        f = self.path / f"eviddata_{hashlib.sha256(data).hexdigest()[:12]}.bin"
        f.write_bytes(data)
        args = ["attach", "--json", event_id, str(f)]
        if note:
            args += ["--note", note]
        code, out, err = self.run(args)
        if code != 0:
            raise AssertionError(f"attach failed (exit {code}): {err}")
        return json.loads(out.strip().splitlines()[-1])["blob_hash"]

    def relocate(self, hash_: str, uri: str):
        code, out, err = self.run(["blob", "relocate", hash_, uri])
        assert code == 0, err

    def evidence(self, event_id: str, **view):
        args = ["evidence", "--json", event_id]
        if "as_of" in view and view["as_of"] is not None:
            args += ["--as-of", str(view["as_of"])]
        code, out, err = self.run(args)
        assert code == 0, err
        result = []
        for line in out.splitlines():
            line = line.strip()
            if line:
                result.append(json.loads(line))
        return result

    def reject(self, *args):
        code, out, err = self.run(list(args))
        return code, out, err

    def event_count(self) -> int:
        code, out, err = self.run(["log", "--json"])
        if code != 0:
            return 0
        return len([l for l in out.splitlines() if l.strip()])

    def db_counts(self) -> dict:
        from evlog import store as store_mod

        con = store_mod.connect(self.path / ".ledger" / "ledger.duckdb")
        try:
            counts = {}
            for t in ("events", "edges", "evidence", "blobs", "blob_locations"):
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            max_seq = con.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM ("
                "SELECT seq FROM events UNION ALL SELECT seq FROM evidence)"
            ).fetchone()[0]
            counts["max_seq"] = max_seq
            return counts
        finally:
            con.close()
