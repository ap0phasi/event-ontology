# ev — an append-only event ledger for plans, decisions and actions

`ev` records everything about a body of work — noticing a need, asking a
question, deciding, intending, doing, correcting a mistake — as immutable
events in a causal DAG. There is no mutable to-do state anywhere. "What is
open", "what is blocked" and "what did I believe on 1 September" are all
derived by pure functions over the events.

It is designed to be used from the terminal by one human and several agents,
for anything from personal life tracking to large engineering programs. See
[`ev an append-only event ledger for plans, decisions and actions.md`](<./ev%20an%20append-only%20event%20ledger%20for%20plans%2C%20decisions%20and%20actions.md>)
for the full original design spec that this implementation follows.

If you want an agent to maintain a ledger for you conversationally (e.g.
"catch it up on my week"), see **[Using `ev` with an agent](#using-ev-with-an-agent)**
below — there is an opencode skill that teaches any agent session how to do
this safely.

## Install

```sh
# from this repo
uv tool install --editable .

# or, inside a project that depends on it
uv add evlog
```

This installs the `ev` binary on your `PATH` (`[project.scripts] ev =
"evlog.cli:main"`). Python 3.10+ is required; the only runtime dependency is
`duckdb`.

## Quickstart

```sh
cd ~/wherever/you/want/a/ledger
ev init                                     # creates .ledger/ledger.duckdb here

n=$(ev add notice "legacy simulator is too slow")
d=$(ev add decide "rebuild with parallel execution" --fulfils "$n")
i=$(ev add intend "implement the parallel simulator" --from "$d")
ev do "$i" "first parallel run matches legacy output"

ev status "$n"     # fulfilled
ev ls               # open items (default: ready/blocked/stuck)
ev log               # everything, in order, with status
```

`ev` discovers `.ledger/ledger.duckdb` by walking up from the current
directory, the same way `git` finds `.git`. You can also point at a specific
ledger with `--db PATH` or the `EV_DB` environment variable, and set the
recorded actor with `EV_ACTOR` (default: OS username).

## Core model

Two tables hold everything that has semantics: `events` and `edges`. Evidence
and a blob catalogue sit beside the graph and carry **no** semantics — you can
attach a file to an event and it will never change that event's status.

### Events

Every event has an `id` (sha256 of its canonical fields — never chosen by
you), a `kind`, free-text `content`, an `actor`, and two timestamps:
`occurred_at` (when it happened, settable with `--at`) and `recorded_at` (when
it was written, always real wall-clock time). Ids display as their first 8
hex characters; any unique prefix of 4+ characters resolves.

Three **opening kinds** can be open or closed:

| kind | meaning |
| --- | --- |
| `notice` | something may need doing |
| `ask` | a question needing an answer |
| `intend` | a commitment to do something |

Every other kind is an inert occurrence. Conventional (not enforced) kinds:
`option`, `decide`, `do`, `answer`, `learn`, `drop`, `retract`. You can invent
your own free-form kinds (must match `^[a-z][a-z0-9_-]{0,31}$`) — only the
three opening kinds above are special-cased.

### Edges (closed set of five labels)

Every edge points from the *new* event to an *existing* one:

| label | meaning |
| --- | --- |
| `from` | arose from / because of / provenance (the default kind of link) |
| `needs` | cannot be done until the target is fulfilled |
| `fulfils` | accomplishes or answers the target (closes it successfully) |
| `drops` | abandons or replaces the target (closes it unsuccessfully) |
| `retracts` | the target was a mistake — remove it from belief (history is untouched) |

### Status is derived, never stored

For a live event of an opening kind, status is computed on the fly, first
match wins:

1. **contested** — two or more live closers (only possible across
   incomparable branches, see "Alternate futures" below)
2. **fulfilled** — one closer, via `fulfils`
3. **dropped** — one closer, via `drops`
4. **stuck** — a `needs` target that is dropped or no longer live (nothing
   implicit happens here — dropping a prerequisite does **not** unblock its
   dependents, it makes them visibly `stuck` until you decide what to do)
5. **blocked** — a `needs` target that isn't fulfilled yet
6. **ready** — otherwise

Non-opening events always show status `-`. A retracted event shows
`retracted` (hidden unless you pass `--include-retracted`).

### Mistakes and alternate futures

There is no edit and no delete. To take back a mistake, `ev retract ID
"why"` — this does not rewrite history (`ev status X --as-of <old seq>` still
shows what you believed then), it only changes the present.

Because every validation rule is judged against an event's own causal past
(the ancestors of its edge targets), two branches that don't reference each
other can each close the same question differently — that's an **alternate
future**, not an error. The whole-ledger view reports it as `contested`; `ev
status X --line <head>` shows you one branch at a time; `ev diff A B` shows
what separates two heads; `ev tips` lists all current heads.

### Views and rewinding

A view is a down-closed set of events. Every query command accepts:

- `--as-of N` — only events/evidence with `seq <= N`
- `--as-of ISO8601` — only events/evidence recorded at or before that instant
  (must carry an explicit UTC offset or `Z`)
- `--line ID` — only the causal ancestors of `ID`, plus `ID` itself

These combine by intersection. No selector means the whole ledger.

### Evidence and blobs

Evidence is supporting material (files, logs, screenshots) attached to an
event by reference. It never affects status and can be attached at any time,
including long after the event was recorded. Moving where the bytes live
(`ev blob relocate`) never changes any id.

## CLI reference

Global flags on every command: `--db PATH`, `--json`, `--full-ids`.
Query commands additionally accept `--as-of` and `--line`.

| command | behaviour |
| --- | --- |
| `ev init` | create `.ledger/ledger.duckdb` in the cwd |
| `ev add KIND CONTENT [--from ID]... [--needs ID]... [--fulfils ID]... [--drops ID]... [--retracts ID]... [--at ISO] [--actor NAME] [--evidence FILE]...` | the only primitive write. `CONTENT` of `-` reads stdin |
| `ev do ID TEXT` | sugar for `add do TEXT --fulfils ID` |
| `ev answer ID TEXT` | sugar for `add answer TEXT --fulfils ID` |
| `ev drop ID TEXT` | sugar for `add drop TEXT --drops ID` |
| `ev retract ID TEXT [--descendants] [--dry-run]` | sugar for `add retract TEXT --retracts ID`; `--descendants` also retracts every live descendant in one transaction |
| `ev supersede ID KIND TEXT [edge flags]` | sugar for `add KIND TEXT --drops ID` plus any extra edges |
| `ev show ID` | the event, its edges, its status, the status of each `needs` target, and its evidence |
| `ev status ID` | status only |
| `ev log [--kind K] [--limit N]` | all events in seq order with status |
| `ev ls [--status S]... [--kind K] [--include-retracted]` | filter by status (default: `open` = ready/blocked/stuck) |
| `ev find QUERY [--status S]... [--kind K] [--include-retracted]` | case-insensitive substring search over `content` (not status-scoped by default — searches everything unless `--status` is given) |
| `ev why ID` | ancestors, with depth |
| `ev then ID` | descendants, with depth |
| `ev tips` | live events with no children in the view |
| `ev diff A B` | events only in `line(A)`, and only in `line(B)` |
| `ev verify [--blobs]` | offline integrity check; exit 3 on failure; `--blobs` re-hashes stored blobs |
| `ev sql QUERY` | run arbitrary **read-only** SQL against the ledger (`-` reads the query from stdin); see [Querying directly](#querying-directly) |
| `ev export [FILE]` / `ev import FILE` | JSONL interchange; merging two ledgers is a set union |
| `ev attach EVENT FILE [--note TEXT]` | attach a file as evidence |
| `ev evidence EVENT [--as-of N\|ISO]` | list an event's evidence |
| `ev blob get HASH --out PATH` | copy stored bytes out |
| `ev blob relocate HASH URI` | record that the bytes also live elsewhere (copies nothing) |

Exit codes: `0` ok · `1` rejected/not found/ambiguous · `2` usage error ·
`3` verify failed · `4` ledger busy (DuckDB allows one writer at a time; `ev`
retries for 5s before giving up).

Default output is tab-separated: `id  seq  status  kind  content`. With
`--json`, one JSON object per line with stable keys (`id, seq, kind, content,
actor, occurred_at, recorded_at, status, edges`).

## Querying directly

`ev find` covers simple keyword search, but you are not limited to the
documented subcommands. `ev sql QUERY` opens the ledger with
`duckdb.connect(path, read_only=True)` and runs whatever you give it —
DuckDB itself rejects any `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`DROP` on a
read-only connection (`InvalidInputException`), so there's no need to
sandbox or allowlist query text; the append-only guarantee holds regardless
of what SQL you write. Use it freely for joins, aggregates, or just poking
around:

```sh
ev sql "SELECT kind, count(*) FROM events GROUP BY kind ORDER BY kind"
ev sql --json "SELECT e.id, e.content FROM events e
                JOIN edges g ON g.src = e.id
                WHERE g.label = 'needs'"
```

Schema: `events(seq, id, kind, content, actor, occurred_at, recorded_at)`,
`edges(src, dst, label)`, `evidence(seq, id, event_id, blob_hash, note,
actor, recorded_at)`, `blobs(hash, media_type, size, recorded_at)`,
`blob_locations(hash, uri, recorded_at)`. Note that `status` is **not** a
column anywhere — it's derived (`ev show`/`ev status`/`reference.py`), so raw
SQL sees the graph but not computed status directly.

This is also the intended path for future smarter search: v1 ships only
plain substring matching (`ev find`), but DuckDB has a vector-similarity-search
extension (`VSS`). A future version could maintain an embeddings table
(populated out-of-band, since computing embeddings isn't this tool's job)
and query it with ordinary SQL via `ev sql`, or formalize that into `ev find
--semantic` later — either way, `ev sql` already gives you (and any agent)
that door today without waiting on new CLI surface.

## Using `ev` with an agent

An opencode skill at `~/.config/opencode/skill/event-ledger/SKILL.md` (or see
`docs/event-ledger-skill.md` in this repo for the same content) teaches any
agent session, in any project, how to:

- find or create the right ledger,
- search for events that a vague statement ("finally shipped the API
  redesign") probably refers to before writing anything new (with `ev find`
  or `ev sql`), avoiding duplicate/contradictory events,
- pick the right kind and edges for new events,
- close things out with `do`/`answer`/`drop` instead of creating orphaned
  records,
- use `retract` instead of ever trying to edit or delete,
- summarize a time range back to you in plain language instead of dumping
  JSON,
- query the ledger directly with `ev sql` when the documented commands don't
  cover what it needs, instead of getting stuck.

Just talk to the agent normally ("catch you up on my week: ...") in a
directory where you want events recorded (or ask it to `ev init` one), and it
will use the skill automatically.

## Development

```sh
uv sync
uv run pytest -q
```

The source lives under `src/evlog/`:

| module | responsibility |
| --- | --- |
| `model.py` | types, canonical content-hashing, structural validation |
| `reference.py` | pure-Python reference derivation of status/views (the executable spec) |
| `store.py` | DuckDB schema, `append()`, causal-past validation, transactions |
| `blobs.py` | `BlobStore` protocol, `LocalBlobStore`, evidence writes |
| `views.py` | DuckDB-backed view selection (recursive CTEs) and status derivation |
| `interchange.py` | JSONL export/import and offline `verify` |
| `cli.py` | argparse CLI wiring all of the above together |

Tests (`tests/`) run the real CLI end-to-end:

- `test_scenarios.py` — the S1–S10 scenarios from the spec (lifecycle,
  rewinding, blocking, dropped prerequisites, retraction, alternate futures,
  rejections, concurrency, merging, evidence)
- `test_properties.py` — Hypothesis property tests P1–P11 (hash determinism,
  acyclicity, append-only, replay, differential SQL-vs-reference derivation,
  rewind-equals-prefix-replay, merge-is-union, clean rejection, evidence
  inertness/idempotency, blob identity-vs-location)
- `test_cli_extra.py` — sugar/explicit equivalence, env vars, `--full-ids`,
  atomic `add --evidence`, id-prefix resolution

There is no `UPDATE`, `DELETE`, or `DROP TABLE`/`DROP SEQUENCE` anywhere in
`src/evlog/` — a test (`test_p3_no_mutating_sql_in_source`) enforces this
statically. All writes are inserts through `store.append()` and
`blobs.attach*()`.

See `AGENTS.md` for conventions when working on this codebase itself.
