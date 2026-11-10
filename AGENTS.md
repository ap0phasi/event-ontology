# Agent instructions for this repository

This repo is the source code for `ev`, an append-only event ledger CLI (see
`README.md` for user-facing docs and the design spec
`ev an append-only event ledger for plans, decisions and actions.md` for the
full original build spec). Read the spec before making any behavioral change
— it is the source of truth for semantics, not this file.

## Invariants that must never be violated

- **Append-only.** There must be no `UPDATE`, `DELETE FROM`, `DROP TABLE`, or
  `DROP SEQUENCE` anywhere in `src/evlog/`. All writes are inserts through
  `store.append()` / `store.append_no_txn()` and `blobs.attach_file()` /
  `blobs.attach_evidence_row()`. `tests/test_properties.py::test_p3_no_mutating_sql_in_source`
  enforces this statically — do not weaken or bypass it.
- **Ids are content hashes**, computed by `model.compute_event_id` /
  `model.compute_evidence_id` over the exact canonical-JSON rules in the spec
  (§2). Never assign an id directly; never change an existing row's fields.
- **Status is derived, never stored.** Don't add a `status` column or cache
  anywhere. All derivation lives in `reference.py` (the pure-Python executable
  spec) and is reused by `views.py` (DuckDB-backed) and `store.py` (append-time
  validation against an event's causal past).
- **Validation happens in an event's own causal past**, not the whole ledger.
  This is what allows alternate futures (see spec §3–4). Don't "fix" a branch
  divergence by checking the whole ledger instead of the ancestor closure of
  the new event's edge targets.
- **DuckDB timestamps must be UTC.** DuckDB silently converts timezone-aware
  Python `datetime`s to the *local* session timezone before storing them as
  naive `TIMESTAMP`. The schema sets `SET TimeZone='UTC';` in
  `store.SCHEMA_SQL` on every connection — do not remove this, and do not add
  a new connection path that skips it, or every timestamp/id will silently
  drift by the local UTC offset the moment this runs somewhere other than
  UTC+0.
- **Default function arguments must not capture `sys.stdout`.** Use
  `out: TextIO | None = None` and resolve `out = out or sys.stdout` inside the
  function body. A default of `out=sys.stdout` binds the stream object at
  *import time*, which silently breaks any caller (tests, future features)
  that redirects stdout, and already caused a real bug here once.

## Workflow

```sh
uv sync            # install/update the venv
uv run pytest -q   # run the full test suite
uv run ev ...       # exercise the CLI directly
```

- Run `uv run pytest -q` before considering any change done. All tests must
  pass (38 at time of writing: scenarios S1–S10, properties P1–P11, `ev
  find`/`ev sql` coverage, plus other CLI extras).
- When changing derivation semantics, update `reference.py` first (it's the
  spec), then re-run `test_properties.py::test_p5_differential_status` to
  confirm `views.py`'s DuckDB-backed derivation still agrees with it.
- When adding a new CLI command or flag, add it to the README's CLI reference
  table and consider whether it needs a scenario test.
- Prefer extending the Hypothesis-driven random-valid-ledger builder in
  `tests/test_properties.py::build_random_valid_ledger` over writing bespoke
  fixtures, when a new property needs a ledger to run against.

## The user-facing "talk to an agent about my week" skill

There is a separate opencode skill (not part of this package) that teaches
agent sessions *using* the `ev` binary — as opposed to developing it — how to
record/update a user's events conversationally without creating duplicates.
Its canonical copy lives at `~/.config/opencode/skill/event-ledger/SKILL.md`
and a tracked copy for reference/version-control lives at
`docs/event-ledger-skill.md` in this repo. If you change `ev`'s CLI surface
(command names, flags, output shapes), update both copies to match.
