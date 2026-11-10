# ev: an append-only event ledger for plans, decisions and actions

*Build spec v0.2. Stack: Python, uv, DuckDB. Interface: CLI only. Audience: a build agent.*

## 1. Purpose and principles

`ev` records everything about a body of work (noticing a need, asking a question, deciding, intending, doing, correcting a mistake) as immutable events in a causal DAG. There is no mutable to-do state anywhere. "What is open", "what is blocked" and "what did I believe on 1 September" are derived by pure functions over the events.

It is used from the terminal by one human and several agents.

The build must be checkable against these principles:

1. **Everything is an event.** The graph is two tables: `events` and `edges`. Evidence and a blob catalogue sit beside it and carry no semantics (section 2).
2. **Append-only.** All writes are inserts through `store.py` (`append` for events, `attach` for evidence and blobs). No `UPDATE`, `DELETE` or `DROP` statement exists in package source (a test scans for this).
3. **Acyclic by construction.** An event can only point at events that already exist, and ids are content hashes, so a cycle cannot be expressed.
4. **State is derived, never stored.** Status is a function of (events, view).
5. **Flat.** `kind` is a free-form token. Only three kinds carry semantics. Edge labels are a closed set of five.
6. **Validity is judged in an event's own causal past.** This is what lets alternate futures disagree without corrupting each other.
7. **Rewinding is free.** A view is a down-closed set of events. Every query takes a view.
8. **Evidence is inert.** An event has evidence; attaching it never changes any derived state, and it can be added later without editing the event.

Where this spec is ambiguous, stop and ask rather than choose. Verify DuckDB behaviours (recursive CTEs, sequences, cross-process locking) by running them against the installed version rather than assuming.

## 2. Data model

### Event

| field | type | notes |
| --- | --- | --- |
| `id` | text | sha256 hex of the canonical form below. Never assigned by the caller. |
| `seq` | int | local insertion order, one sequence shared with evidence rows, gapless across both. **Not** part of the hash. |
| `kind` | text | matches `^[a-z][a-z0-9_-]{0,31}$` |
| `content` | text | free text, non-empty |
| `actor` | text | who recorded it: `john`, `agent:claude`. Default from `EV_ACTOR`, else OS username. |
| `occurred_at` | UTC timestamp | when it happened. Defaults to `recorded_at`. Must be `<= recorded_at`. |
| `recorded_at` | UTC timestamp | when it was written. Must be `>=` the `recorded_at` of every parent. On append use `max(now, parents' recorded_at)`. |
| edges | list of (label, dst) | immutable, written with the event |

**Canonical form for hashing:** JSON with keys `actor, content, edges, kind, occurred_at, recorded_at`; `edges` is a list of `[label, dst]` pairs sorted ascending; timestamps formatted `YYYY-MM-DDTHH:MM:SS.ffffffZ`; serialised with `sort_keys=True, separators=(",", ":"), ensure_ascii=False`, UTF-8 encoded.

Ids display as the first 8 hex characters. Any unique prefix of at least 4 characters resolves to an id. An ambiguous or unknown prefix is an error.

### Edge labels (closed set, direction is new event to existing event)

| label | meaning |
| --- | --- |
| `from` | arose from, because of, provenance. The default. |
| `needs` | cannot be done until the target is fulfilled |
| `fulfils` | accomplishes or answers the target (closes it successfully) |
| `drops` | abandons, declines or replaces the target (closes it unsuccessfully) |
| `retracts` | the target was a mistake; remove it from belief (history is untouched) |

### Kinds

Kinds are convention, with one exception. Three **opening kinds** are things that can be open or closed:

- `notice`: something may need doing
- `ask`: a question needing an answer
- `intend`: a commitment to do something

These live in one constant, `OPENING_KINDS`, and nowhere else. Every other kind is an inert occurrence. Conventional (not enforced) kinds: `option`, `decide`, `do`, `answer`, `learn`, `drop`, `retract`.

### Evidence and blobs

Evidence is supporting material: files, logs, screenshots, query results. It is **never an event** and **never affects derived state**: attaching evidence cannot change any status, which is why it sits outside the graph. An event *has* evidence. Evidence is attached by reference and can be added at any time without editing the event.

Three append-only tables (DDL in section 7):

| table | holds |
| --- | --- |
| `blobs` | Catalogue of artifacts: `hash`, `media_type`, `size`, `recorded_at`. `hash` is the sha256 hex of the bytes, streamed in chunks. This is the immutable identity. |
| `blob_locations` | Where the bytes live: `hash`, `uri`, `recorded_at`. The latest row for a hash wins. Moving a blob appends a row; no event or evidence id changes. |
| `evidence` | One row per attachment: `id`, `event_id`, `blob_hash`, `note`, `actor`, `recorded_at`, `seq`. |

- `evidence.id` is the sha256 of the canonical JSON of `{actor, blob_hash, event_id, note, recorded_at}`, using the same canonical rules as events. Re-attaching an identical row is a no-op.
- `evidence.seq` comes from the **same sequence as `events.seq`**. Events and evidence share one total order, gapless across both. That is what lets `--as-of N` answer "what evidence did I have then?".
- Event ids do **not** commit to their evidence, including evidence attached at creation. There is one mechanism (the table), not two.
- No withdrawal in v1. A wrong attachment is corrected by an ordinary event (for example `learn`, with a `from` edge to the event) that says so.
- `BlobStore` is a small protocol: `put(stream) -> (hash, size)`, `open(hash)`, `exists(hash)`. v1 ships `LocalBlobStore` under `.ledger/blobs/<hash[:2]>/<hash>`, with locations recorded as `local:<hash[:2]>/<hash>` (forward slashes on every OS). An S3 or DuckLake-backed store is a later implementation behind the same protocol.

## 3. Derived semantics

A **view** V is a set of events closed under ancestors (see section 5). All derivations below are relative to V and are pure functions of V. Nothing is cached in the store.

1. **live(E, V):** E is in V and no live event in V has a `retracts` edge to E. Compute by iterating events in V in **descending seq**; retractors always have higher seq than their targets, so they are already resolved. Retracting a retraction therefore restores the original.
2. **closers(E, V):** live events in V with a `fulfils` or `drops` edge to E.
3. **status(E, V)** for a live event of an opening kind, first match wins:
   - `contested`: two or more closers. (They can only be on incomparable branches; section 4 forbids it within one line.)
   - `fulfilled`: one closer, via `fulfils`.
   - `dropped`: one closer, via `drops`.
   - `stuck`: it has a `needs` edge to a target P that is dropped or not live in V. It can never complete without a new decision.
   - `blocked`: it has a `needs` edge to a target P that is not yet fulfilled.
   - `ready`: otherwise.
4. **open** means `ready`, `blocked` or `stuck`.
5. Events of non-opening kinds have status `-`. Retracted events have status `retracted` (shown only with `--include-retracted`).

Nothing is implicit: dropping a prerequisite does **not** unblock its dependents. It makes them `stuck`, which is a visible prompt for a decision.

## 4. Append validation

`append` runs in one transaction. Let **past** be the ancestor closure of the new event's edge targets (the new event's own causal past). Every rule is evaluated in that past, not in the whole ledger. All violations exit with code 1 and **write nothing**.

1. `kind` matches the pattern; `content` non-empty.
2. Every edge target exists. Labels are in the closed set. No duplicate (label, dst). No self-edges.
3. `needs` target: opening kind, live in past, not dropped in past.
4. `fulfils` and `drops` target: opening kind, live in past, and **open in past** (not already closed there). Closing a `blocked` item is allowed (reality is not gated); the CLI prints a warning.
5. One event may not both `fulfils` and `drops` the same target.
6. `retracts` target: live in past.
7. `--at` must be ISO 8601 with an explicit offset or `Z` (naive timestamps rejected) and not in the future.
8. If the computed id already exists, return it and write nothing (idempotent).

Because rule 4 uses the event's own past, two incomparable branches may each close the same question differently. That is an alternate future, and the whole-ledger view reports it as `contested`.

**Attach validation.** Writing an evidence row requires that the event exists (any event, retracted or not) and that `blob_hash` exists in `blobs`. `recorded_at` is clamped to `max(now, the event's recorded_at)`. An identical row (same computed id) is a no-op that returns the existing id. A failed attach writes nothing, and `ev add --evidence` is atomic: if any attachment fails, the event is not written either.

## 5. Views: rewinding and alternate futures

A view selector yields a down-closed set:

- `--as-of N`: all events with `seq <= N`. Down-closed because parents always have lower seq.
- `--as-of ISO`: all events with `recorded_at <=` the timestamp. Down-closed because of the `recorded_at` rule in section 2.
- `--line ID`: ancestors of ID, plus ID. The causal past of one head. This is how you look at one future.
- Selectors combine by intersection (both are down-closed, so the result is too). No selector means the whole ledger.

There are no stored branches or refs. A branch is a head; two futures are two heads with a shared past. `ev tips` lists heads, and `ev diff A B` shows what separates two lines. If named bookmarks are wanted later, they should be events (kind `mark`), not mutable refs.

View-relative evaluation preserves original belief: a view before a retraction event does not contain it, so it shows the retracted thing as live.

**Evidence in a view.** An evidence row is shown when its event is in the view and the row itself is inside any `--as-of` bound (by seq for an integer, by `recorded_at` for a timestamp). Evidence has no status and never changes one.

## 6. CLI

Binary name `ev`. Standard library `argparse`; no CLI framework dependency.

Global flags on every command: `--db PATH` (else `EV_DB`, else discover `.ledger/ledger.duckdb` by walking up from the cwd like git), `--json`, `--full-ids`.

### Commands

| command | behaviour |
| --- | --- |
| `ev init` | create `.ledger/ledger.duckdb` in the cwd |
| `ev add KIND CONTENT [--from ID]... [--needs ID]... [--fulfils ID]... [--drops ID]... [--retracts ID]... [--at ISO] [--actor NAME]` | the only primitive write. `CONTENT` of `-` reads stdin. Prints the new id. |
| `ev do ID TEXT` | sugar: `add do TEXT --fulfils ID` |
| `ev answer ID TEXT` | sugar: `add answer TEXT --fulfils ID` |
| `ev drop ID TEXT` | sugar: `add drop TEXT --drops ID` |
| `ev retract ID TEXT [--descendants] [--dry-run]` | sugar: `add retract TEXT --retracts ID`. With `--descendants`, also one retraction per live descendant (children via any label), all in one transaction. |
| `ev supersede ID KIND TEXT [edge flags]` | sugar: `add KIND TEXT --drops ID` plus any extra edge flags |
| `ev show ID` | the event, its edges in and out, its status, and the status of each `needs` target |
| `ev status ID` | status only |
| `ev log [--kind K] [--limit N]` | all events in seq order with status |
| `ev ls [--status S]... [--kind K] [--include-retracted]` | filter by status. Default is `open`. Statuses: `ready blocked stuck fulfilled dropped contested open`. |
| `ev why ID` / `ev then ID` | ancestors / descendants, with depth |
| `ev tips` | live events with no children in the view |
| `ev diff A B` | events only in `line(A)`, and only in `line(B)` |
| `ev verify` | integrity check (section 7). Exit 3 on failure. |
| `ev export [FILE]` / `ev import FILE` | JSONL interchange |

All query commands accept `--as-of` and `--line`. Sugar commands must produce events byte-identical to the equivalent explicit `ev add` (tested).

**Evidence commands.**

- `ev attach EVENT FILE [--note TEXT]`: streams FILE through sha256, stores it in the blob store if new, appends the `blobs` and `blob_locations` rows if new, then appends the `evidence` row. Prints the blob hash and the evidence id. Media type comes from `mimetypes`, else `application/octet-stream`.
- `ev add KIND CONTENT … --evidence FILE`: repeatable sugar. Appends the event, then attaches each file, in one transaction.
- `ev evidence EVENT [--as-of N|ISO]`: lists the event's evidence. `--json` keys: `id, event_id, blob_hash, size, media_type, note, actor, recorded_at, seq, location`. `ev show` includes the same list.
- `ev blob get HASH --out PATH`: copies the bytes out of the blob store. Exit 1 if the bytes are unavailable.
- `ev blob relocate HASH URI`: appends a `blob_locations` row recording that the bytes also live at URI. It copies nothing.

### Output

Default output is plain text, one event per line: `id  seq  status  kind  content` (tab-separated, UTC timestamps). With `--json`, one JSON object per line with stable keys: `id, seq, kind, content, actor, occurred_at, recorded_at, status, edges: [{label, dst}]`. `ev add --json` prints `{"id": "<full id>"}`.

Exit codes: 0 ok, 1 rejected or not found or ambiguous, 2 usage error, 3 verify failed, 4 ledger busy.

## 7. Storage, integrity, interchange

Illustrative DDL (check it runs on the installed DuckDB and adjust types as needed):

```sql
CREATE SEQUENCE ev_seq START 1;
CREATE TABLE events (
  seq BIGINT PRIMARY KEY DEFAULT nextval('ev_seq'),
  id VARCHAR UNIQUE NOT NULL,
  kind VARCHAR NOT NULL,
  content VARCHAR NOT NULL,
  actor VARCHAR NOT NULL,
  occurred_at TIMESTAMP NOT NULL,   -- UTC by convention
  recorded_at TIMESTAMP NOT NULL    -- UTC by convention
);
CREATE TABLE edges (
  src VARCHAR NOT NULL, dst VARCHAR NOT NULL, label VARCHAR NOT NULL,
  PRIMARY KEY (src, dst, label),
  CHECK (label IN ('from','needs','fulfils','drops','retracts')),
  CHECK (src <> dst)
);
```

Evidence and catalogue DDL (same caveat; `evidence.seq` draws from the same `ev_seq` as events):

```sql
CREATE TABLE blobs (
  hash VARCHAR PRIMARY KEY,          -- sha256 hex of the bytes
  media_type VARCHAR NOT NULL,
  size BIGINT NOT NULL,
  recorded_at TIMESTAMP NOT NULL
);
CREATE TABLE blob_locations (
  hash VARCHAR NOT NULL,
  uri VARCHAR NOT NULL,
  recorded_at TIMESTAMP NOT NULL,
  PRIMARY KEY (hash, uri, recorded_at)
);
CREATE TABLE evidence (
  seq BIGINT PRIMARY KEY DEFAULT nextval('ev_seq'),   -- same sequence as events
  id VARCHAR UNIQUE NOT NULL,
  event_id VARCHAR NOT NULL,
  blob_hash VARCHAR NOT NULL,
  note VARCHAR NOT NULL DEFAULT '',
  actor VARCHAR NOT NULL,
  recorded_at TIMESTAMP NOT NULL
);
```

Referential integrity (`event_id` and `blob_hash` exist) is enforced in `attach` and re-checked by `verify`, not relied on from DuckDB constraints.

**`ev verify`** is fully offline. It recomputes every event and evidence id from stored fields and checks: ids match; `seq` is unique and gapless from 1 across events and evidence together; every edge endpoint exists; every parent has lower `seq` and `<=` `recorded_at` than its child; every evidence row's `event_id` exists, its `blob_hash` is in `blobs`, and its `recorded_at` is `>=` its event's; kinds and labels are valid; and every historical append rule that can be re-evaluated still holds in each event's own past. **`ev verify --blobs`** additionally re-hashes every blob whose bytes it can read: a mismatch fails (exit 3), and an unreachable blob is reported as `unavailable` without failing.

**Interchange.** `ev export` writes one JSON object per line, deterministic byte-for-byte, each with a `type` field: `blob` and `blob_location` rows first, then `event` and `evidence` rows interleaved in seq order (all fields except `seq`, plus edges on events). `ev import` recomputes ids, requires every referenced event and blob to exist in the DB or earlier in the file, skips rows already present, and appends the rest. Blob bytes are not part of the export, only catalogue rows: imported blobs show as `unavailable` under `verify --blobs` until the bytes are synced separately (in prod, blob storage is shared). Because everything is content-addressed, merging two ledgers is set union. Import must be idempotent and order-independent as to the resulting id sets.

**Concurrency.** DuckDB allows one read-write process at a time. Open the connection per command and close it before exiting. On a lock conflict, retry with backoff for up to 5 seconds, then exit 4. Do not use POSIX-only locking.

## 8. Project setup and constraints

The team is mixed Windows and Linux, so everything must be cross-platform.

- Scaffold with the tool, not by hand: `uv init --package evlog`, then `uv add duckdb` and `uv add --dev pytest hypothesis`. Inspect what `uv init` generated and make only the minimal edit needed so that `uv run ev` invokes `evlog.cli:main`.
- Runtime dependency: `duckdb` only (hashing uses stdlib `hashlib`, media types use `mimetypes`). Dev: `pytest`, `hypothesis`.
- No shell scripts, batch files or Makefiles. Everything runs through `uv run`.
- Use `pathlib` for paths. No `fcntl`, no hard-coded separators, no assumptions about the shell.
- Suggested modules: `model.py` (types, canonical hash, validation), `reference.py` (pure-Python derivation, section 9), `store.py` (DuckDB, `append`, `attach`), `blobs.py` (`BlobStore` protocol, `LocalBlobStore`, streaming sha256), `views.py` (view selectors, SQL derivation), `cli.py`.

## 9. Build order

Each step ends with its tests green before the next starts.

1. `model.py` and `reference.py`: canonical hashing (events and evidence), validation, and a **pure-Python reference implementation** of sections 3 to 5 over an in-memory list of events and evidence. This is the executable spec. Properties P1, P2, P6, P9.
2. `store.py`: DuckDB schema for events and edges, `append()`, transactions, rejection leaves no trace. Properties P3, P8.
3. `blobs.py` and `attach()`: the `BlobStore` protocol, `LocalBlobStore`, streaming sha256, the catalogue and evidence tables on the shared `seq`. Properties P10, P11.
4. `views.py`: SQL derivation (recursive CTEs for ancestors) **differentially tested against the reference model**, including evidence visibility under views. Property P5.
5. `cli.py`: all commands, `--json`, exit codes, sugar equivalence.
6. `export`, `import`, `verify`. Properties P4, P7.
7. Scenario tests S1 to S10 through the real CLI.

## 10. Tests

Tests exist to convey the intent as much as to catch bugs. If an implementation passes the properties but feels like a to-do app with an event table bolted on, it has missed the point.

### 10.1 Property tests (Hypothesis)

Generate random *valid* ledgers by driving `append` with random choices among currently legal edges.

- **P1 Hash determinism.** Same fields give the same id. Changing any single field, or the edge set, changes the id. Edge input order does not matter. The same holds for evidence ids.
- **P2 Acyclic by construction.** For every edge, `dst.seq < src.seq`. `verify` passes after any sequence of valid appends.
- **P3 Append-only.** After any append sequence, every row read earlier (events, edges, evidence, blobs, locations) is unchanged, and row counts only grow. A static test asserts no `UPDATE`, `DELETE` or `DROP` appears in package source.
- **P4 Replay.** Export, then import into an empty DB: identical ids, identical seq order, identical derived status for every event under the whole-ledger view, and identical evidence per event.
- **P5 Differential.** For random ledgers and random views (`--as-of`, `--line`, both), SQL-derived status equals the reference model's status for every event.
- **P6 Rewind equals prefix replay.** For any N, status computed with `--as-of N` on the full ledger equals status computed on a ledger containing only the first N rows (events and evidence). The evidence listed under `--as-of N` is exactly the rows with `seq <= N`.
- **P7 Merge is union.** For two ledgers sharing a prefix and then diverging, importing each into the other (in either order) yields the same id sets (events, evidence, blobs) and the same statuses; re-importing changes nothing.
- **P8 Rejection is clean.** Any rejected append or attach leaves the DB byte-identical in content (row counts and max seq in every table).
- **P9 Evidence is inert.** Attaching arbitrary evidence to arbitrary events leaves every derived status, in every view, unchanged.
- **P10 Evidence is idempotent and ordered.** Re-attaching an identical row returns the same id and writes nothing. `seq` is gapless across events and evidence together.
- **P11 Identity versus location.** Appending a `blob_locations` row changes no event id, evidence id or status. Corrupting a stored blob's bytes makes `verify --blobs` fail (exit 3); a missing blob is reported `unavailable` and does not fail.

### 10.2 Scenario tests (the vibe)

Scenarios run the real CLI in a temp directory. Helper sketch: `add(kind, text, **edges) -> id`, `status(id, **view) -> str`, `ids("ls", "--status", "ready") -> set`.

**S1. The basic lifecycle, and rewinding it.**

```python
n = add("notice", "legacy simulator is too slow")
d = add("decide", "rebuild with parallel execution", fulfils=[n])
i = add("intend", "implement the parallel simulator", **{"from": [d]})
x = add("do", "first parallel run matches legacy output", fulfils=[i])

assert status(n) == "fulfilled" and status(i) == "fulfilled"
assert status(n, as_of=seq(n)) == "ready"        # before the decision
assert status(i, as_of=seq(d)) is None            # i did not exist yet
assert status(i, as_of=seq(i)) == "ready"
```

The notice was never edited. It is fulfilled only because a later event points at it.

**S2. Needing information before acting.**

```python
q = add("ask", "is JAX or torch better for this model?")
i = add("intend", "port the simulator", needs=[q])
assert status(q) == "ready" and status(i) == "blocked"
assert ids("ls", "--status", "ready") == {q}      # blocked work is not ready

add("answer", "JAX; 3x faster in the benchmark", fulfils=[q])
assert status(q) == "fulfilled" and status(i) == "ready"
```

**S3. A dropped prerequisite makes dependents stuck, then supersede.**

```python
a = add("intend", "provision the A100")
b = add("intend", "run the benchmark", needs=[a])
add("drop", "budget refused", drops=[a])
assert status(a) == "dropped" and status(b) == "stuck"
assert ids("ls", "--status", "stuck") == {b}
assert b not in ids("ls", "--status", "ready")   # nothing implicit

b2 = supersede(b, "intend", "run the benchmark on the laptop")
assert status(b) == "dropped" and status(b2) == "ready"
assert b in ids("why", b2)                        # history is intact
```

**S4. Retraction rewrites the present, never the past.**

```python
i = add("intend", "regenerate the report")
x = add("do", "regenerated report", fulfils=[i])    # actually the wrong file
r = add("retract", "wrong input file", retracts=[x])

assert status(i) == "ready"                       # reopened in the present
assert status(i, as_of=seq(x)) == "fulfilled"     # what I believed then
assert status(i, as_of=seq(r)) == "ready"

add("retract", "it was the right file after all", retracts=[r])
assert status(i) == "fulfilled"                   # retracting a retraction restores
```

**S5. Alternate futures.** The core scenario. Two incompatible decisions coexist, are inspected separately, then one is resolved.

```python
q  = add("ask", "which storage engine?")
o1 = add("option", "DuckDB",   **{"from": [q]})
o2 = add("option", "Postgres", **{"from": [q]})
d1 = add("decide", "DuckDB",   **{"from": [o1]}, fulfils=[q])
i1 = add("intend", "write the schema", **{"from": [d1]})
d2 = add("decide", "Postgres", **{"from": [o2]}, fulfils=[q])   # allowed: q is open in d2's past
i2 = add("intend", "provision a server", **{"from": [d2]})

assert status(q) == "contested"                   # whole ledger: two live closers
assert status(q, line=i1) == "fulfilled" and i2 not in ids("log", line=i1)
assert status(q, line=i2) == "fulfilled" and i1 not in ids("log", line=i2)
assert diff(i1, i2) == ({o1, d1, i1}, {o2, d2, i2})
assert ids("tips") == {i1, i2}

# Sequentially deciding twice inside ONE line is rejected
reject("add", "decide", "changed my mind", "--from", d1, "--fulfils", q)

# Resolve: commit to DuckDB
retract(d2, "going with DuckDB", descendants=True)   # also retracts i2
assert status(q) == "fulfilled" and ids("ls", "--status", "ready") == {i1}
assert status(q, as_of=seq(i2)) == "contested"       # the fork is still in the history
```

**S6. Rejections are specific and leave no trace.** Each case is a separate test asserting exit code 1 and an unchanged event count.

- unknown id, and an ambiguous prefix
- kind `Bad Kind`
- `--needs` pointing at a `do` event (not an opening kind)
- `--fulfils` on something already fulfilled in the new event's past
- `--fulfils` and `--drops` on the same target in one event
- `--at` in the future, and `--at` without an offset
- `--retracts` on an event not live in its past

**S7. Agents sharing a ledger.** Two processes each append 50 events concurrently (each tagged with its own `--actor`). All 100 land, `ev verify` exits 0, and no process fails with a raw DuckDB lock traceback. Exit 4 is acceptable only after the 5-second retry budget.

**S8. Merging ledgers.** Ledger A is exported and imported into B. Each then gets independent new events. Exporting each and importing into the other gives both ledgers the same id set and the same statuses. Importing again is a no-op. Evidence attached on either side to shared events appears on both, with its blobs marked unavailable until the bytes are synced.

**S9. Evidence arrives late and stays inert.**

```python
i = add("intend", "benchmark the parallel simulator")
x = add("do", "benchmark finished", fulfils=[i])
before = all_statuses()

h = attach(x, b"timings: 1 thread 41.2s, 8 threads 6.1s", note="raw timings")
assert all_statuses() == before                      # evidence never changes state
assert [e.blob_hash for e in evidence(x)] == [h]
assert evidence(x, as_of=seq(x)) == []               # not attached yet at that point

ids_before = ids("log")
relocate(h, "s3://team-bucket/bench/results.csv")     # a new blob_locations row only
assert ids("log") == ids_before                      # identity is the hash, not the place
assert [e.blob_hash for e in evidence(x)] == [h]
```

**S10. Evidence cannot dangle, and tampering shows.** Each case is a separate test.

- `attach` to an unknown event: exit 1, nothing written.
- `add … --evidence missing.csv` where the file does not exist: exit 1, and the event is **not** written (the operation is atomic).
- Overwrite a stored blob's bytes in the local store: `ev verify` still exits 0 (it is offline), `ev verify --blobs` exits 3.
- Delete a stored blob's file: `ev verify --blobs` exits 0 and reports it `unavailable`; `ev blob get HASH` exits 1.

## 11. Out of scope for v1

- Due dates and scheduling. A date is content; it carries no semantics.
- Named branches or refs (see section 5).
- Priorities, tags, assignees and estimates. These are either content or kinds by convention.
- A TUI or web UI. Agents and the human use the CLI.
- Cascading retraction as automatic behaviour. It is explicit, via `--descendants`.
- Withdrawing or retracting evidence. A wrong attachment is corrected by an ordinary event.
- Remote blob stores (S3, DuckLake) and attaching by URI or provider-pinned reference. v1 hashes local files; remote stores plug in behind `BlobStore`.
- Deleting blob content, and garbage collection of blobs.
