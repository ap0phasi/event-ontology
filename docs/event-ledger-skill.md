---
name: event-ledger
description: Use when the user wants to talk through their week, a project, or their life and have an agent record, update, or review it in their `ev` event ledger — e.g. "catch you up on my week", "log this", "mark X as done", "what's still open", "what did I decide about Y", or any statement describing something noticed, asked, decided, intended, done, dropped, or that turned out to be wrong. Also use whenever `ev`, "event ledger", or "the ledger" is mentioned explicitly. Do not use for unrelated task-tracking tools (Jira, Linear, todo.txt, etc.) unless the user says they mean the `ev` ledger.
---

# Talking to the `ev` event ledger

`ev` is an append-only CLI event ledger (binary: `ev`, built from the
`event-ontology` repo). There is no edit and no delete — every "update" is a
new event that points at an old one. Your job in this skill is to turn the
user's natural-language description of their week/project/life into the
*right* sequence of `ev` commands: matching existing open items instead of
duplicating them, picking sensible kinds and edges, and never guessing when
you could check.

If `ev --help` fails (binary not installed), tell the user and stop — don't
try to reimplement the model yourself in some other file.

## The model, just enough to act correctly

- **Opening kinds** (`notice`, `ask`, `intend`) can be open or closed.
  Everything else (`do`, `decide`, `option`, `answer`, `learn`, `drop`,
  `retract`, or any free-form kind) is an inert record of something that
  happened.
- **Edges** (new event → existing event): `from` (provenance/context, the
  default link), `needs` (blocked on), `fulfils` (closes successfully),
  `drops` (closes unsuccessfully), `retracts` (this was a mistake, undo it —
  history is untouched, only the present changes).
- **Status** (`ready`, `blocked`, `stuck`, `fulfilled`, `dropped`,
  `contested`) is computed, never stored. `ev ls` defaults to showing only
  open work (`ready`/`blocked`/`stuck`).
- **A time mark (`ev when <id> <time>`) is the event's scheduled instant.**
  One effective time per event, latest-wins — correcting a time is
  appending a new mark, never editing. An event displays/sorts by its
  current mark if it has one, else its `occurred_at`.
- Ids are content hashes; the CLI prints/accepts 8-character prefixes. Any
  unique prefix of 4+ characters also resolves.
- **There is no edit.** If the user says something was wrong, use
  `ev retract <id> "why"` on the specific wrong event — never try to find a
  way to mutate it.

Full command/flag reference: run `ev <command> --help`, or see the
`event-ontology` repo's README. Don't guess at flags you're unsure of —
check `--help` rather than inventing syntax. You are **not limited to the
documented subcommands** — `ev sql QUERY` runs arbitrary SQL against the
ledger and is genuinely safe to reach for whenever a question doesn't map
cleanly onto `ls`/`log`/`find`: DuckDB opens that connection read-only and
rejects any write itself, so there's no special care needed beyond writing
correct SQL. Don't avoid it out of caution; the tool is designed so you
don't have to be careful here.

## Step 0: find or create the ledger

`ev` discovers `.ledger/ledger.duckdb` by walking up from the current
working directory, like `git`. Before doing anything else in a new
conversation:

1. Run `ev log --json --limit 1` (cheap, harmless) from the directory the
   user is talking in.
   - If it succeeds (even with zero output), a ledger was found — proceed.
   - If it fails because no ledger exists, **ask the user** where they want
     one (their home directory for life-tracking, the current project root
     for engineering work) rather than silently `ev init`-ing wherever your
     shell happens to be. Once they confirm, `cd` there and `ev init`.
2. If the user has a conventional place they keep their ledger (e.g.
   `~/ledger`), prefer that. `--db PATH` or `EV_DB` can always target it
   explicitly if cwd discovery would pick the wrong one.
3. Don't create a second ledger for the same body of work just because the
   shell's cwd changed — if in doubt, ask.

## Step 1: orient before writing anything

Before recording new events, always look at what's already there. This is
the single most important habit in this skill — **skipping it is how you get
duplicate or contradictory events.**

```sh
ev ls --json                     # currently open items (ready/blocked/stuck)
ev log --json --limit 30         # recent history, in order, with status
ev find "keyword" --json         # substring search over ALL event content,
                                  # not status-scoped — use this first when
                                  # you have a specific word/phrase to look for
```

Use `--json` for your own parsing (one object per line: `id, seq, kind,
content, actor, occurred_at, recorded_at, status, edges`). Never hand-parse
the plain-text tab output; it's for humans.

When the user's statement plausibly continues or resolves something already
in the ledger, find it before writing:

- Start with `ev find <keyword>` for whatever noun/phrase the user used
  ("Sarah", "Q3 report", "Okta"). It's a simple case-insensitive substring
  match today — if it misses because of paraphrasing, fall back to scanning
  `ev ls --json` / `ev log --json`, or write a one-off `ev sql` query (e.g.
  joining on `edges` to find things linked to a particular topic). Don't
  skip straight to creating a new event just because the first search came
  up empty — try a second phrasing or broaden the time window first.
- If you need more context on a candidate, `ev show <id> --json` (includes
  its edges, `needs` targets' status, and evidence).
- If exactly one open item is a confident match → close or extend *that*
  one (see Step 2).
- If several items could match → ask the user a short disambiguating
  question, showing short ids and content, e.g. "Did you mean `3a7bd53d
  'rebuild the parallel simulator'` or `d7545344 'rebuild the ingest
  pipeline'`?" Don't guess silently.
- If nothing matches → it's genuinely new; create it (Step 3).

Never create a new, disconnected event for something that is obviously the
continuation of existing open work. A dangling `do` event with no `fulfils`
edge back to the thing it finished is almost always a bug in how you used
the tool, not a valid outcome.

## Step 2: closing out or extending existing items

| user said... | do this |
| --- | --- |
| finished/shipped/completed an open `intend` or `notice` | `ev do <id> "what happened"` |
| answered an open `ask` | `ev answer <id> "the answer"` |
| decided not to / gave up on / abandoned an open item | `ev drop <id> "why"` |
| picked a different approach than planned, replacing an open item | `ev supersede <id> <kind> "text" [--from ...] [--needs ...]` |
| said something they told you before was wrong/a mistake | `ev retract <id> "why"` (and `--descendants` if things built on top of it should go too — ask first if it's not obvious) |
| a new task depends on something still open | `ev add intend "..." --needs <id>` |

After any of these, it's good practice to `ev status <id>` (or re-run `ev
ls`) and fold the result into your confirmation back to the user — don't
just assume it worked.

## Step 3: recording something genuinely new

Pick the kind deliberately:

- **`notice`** — you noticed something that might need doing, but haven't
  committed to anything yet ("the deploy script is flaky").
- **`ask`** — an open question ("should we use JAX or torch?").
- **`intend`** — a commitment to do something ("I'm going to rebuild the
  simulator").
- Anything else (`do`, `decide`, `option`, `answer`, `learn`, `drop`,
  `retract`, or a free-form kind like `meeting`, `review`) — an inert record
  of something that already happened. Use these for narrating things the
  user mentions in passing that don't need to be "open" (a decision already
  made, an option considered, a lesson learned).

Link it to context with `--from <id>` whenever it's a direct continuation of
something you just found in Step 1 — this keeps the causal graph (and
`ev why`/`ev then`) meaningful, and matters for validation (only an event's
own causal past is checked when it later closes something). Don't bother
linking every event to everything that's vaguely related; link what's
actually causally relevant.

```sh
ev add notice "the deploy script is flaky" --json
ev add ask "should we use JAX or torch for this?" --json
ev add intend "rebuild the parallel simulator" --from <notice-id> --json
```

Use `--json` so you can parse the resulting `{"id": "..."}` reliably and
chain follow-up commands (edges, evidence) without re-resolving ids.

## Step 4: times (`ev when` / `ev schedule`)

When the user mentions a real date or time — an appointment, a deadline, a
flight, a trip — don't leave it buried in the content string. Pin it with a
time mark so it shows on the timeline and can go overdue:

```sh
ev when <event-id> 2026-11-13T09:00:00Z --note "vaccination appointment" --json
```

- The time is ISO 8601 with an explicit offset or `Z`, and may be future
  (that's the point — plan ahead) or past (correct a mistaken instant
  without touching the event). `--at` on `ev add` is different: it pins the
  event's own `occurred_at` — when it *happened* — not when it's due.
- **One effective time per event, latest-wins.** A second `ev when` on the
  same event becomes its effective time — correction by append, like
  everything else in this model. A *span* (a trip Jan 2–25) is therefore
  two events: mark the start on the trip event, and record the end as its
  own small inert event (`ev add travel "return home" --from <trip-id>`)
  with its own `when`.
- `ev schedule` is the timeline view — events sorted by effective time
  (current mark if any, else `occurred_at`); in `--json` it adds `when`,
  `when_is_mark` and `overdue` fields. Filters: `--overdue`, inclusive
  `--from-time`/`--to-time`, `--kind`, `--status`, `--line`, `--as-of`;
  `ev ls --overdue` gives the status-scoped "what's late". Overdue means
  *open + has a time mark + that time has passed* — a bare `occurred_at`
  is never an implicit deadline.

## Step 5: evidence (optional, don't force it)

If the user hands you a file, log excerpt, or link that supports something
you just recorded, attach it — it never changes status:

```sh
ev attach <event-id> <file> --note "what this is"
```

Don't invent evidence files; only attach things the user actually gave you.

## Step 6: summarizing a time range ("catch up on my week")

`ev schedule --from-time`/`--to-time` (inclusive) is the dedicated
date-range timeline view; for anything it doesn't cover, `ev sql` filters
on `occurred_at`/`recorded_at` directly rather than fetching everything
and filtering client-side:

```sh
ev sql --json "SELECT * FROM events WHERE occurred_at >= now() - INTERVAL 7 DAY ORDER BY seq"
```

Then narrate the result — don't dump raw JSON at the user. Group by what's
now open vs. resolved, lead with anything `stuck` or `contested` (those need
a human decision; `status` isn't a SQL column, so cross-reference with `ev
ls`/`ev show` for those), and use short ids only if the user might want to
refer back to something.

## Things that will bite you if you forget them

- **Idempotency ≠ "safe to retry blindly".** `ev add` with identical
  kind/content/actor/edges/`--at` *usually* creates a new event anyway,
  because `recorded_at` is real wall-clock time unless you pass `--at` (which
  only pins `occurred_at`). Don't call `ev add` more than once per thing the
  user told you — if you're unsure whether a command landed, check `ev find
  "..."` or `ev log --json` for it before re-running.
- **No edit, no delete.** If you catch yourself wanting to "fix" an event
  you just created with wrong content, `ev retract` it and add a correct one
  — do not look for a hidden update path, there isn't one.
- **Closing something that's `blocked` is allowed but warns.** If `ev
  do`/`ev answer`/`ev drop` prints a warning to stderr about closing a
  blocked item, that's expected behavior (reality isn't gated by the
  ledger's bookkeeping) — mention it to the user rather than treating it as
  a failure.
- **`stuck` is a deliberate prompt, not a bug.** If `ev ls` shows something
  `stuck` (its `needs` target was dropped), don't silently work around it —
  tell the user and ask what they want to do (supersede, drop the stuck
  item too, or re-point it).
- **`contested` means two branches disagree**, usually because you (or a
  past session) recorded conflicting outcomes without linking them. Surface
  it to the user with `ev diff` between the two tips rather than picking a
  side yourself.
- **A failed command writes nothing.** If an `ev add`/`ev attach` exits
  non-zero, nothing was recorded — don't tell the user it was logged, and
  don't assume partial state to clean up.
- **`ev find` is plain substring matching, not semantic search.** It won't
  connect "shipped the redesign" to a stored event that says "finished the
  UI overhaul" unless you also try the words the user is likely to have
  used originally, or fall back to scanning `ev log --json`. If this
  consistently causes misses in practice, that's a signal for a future
  improvement (e.g. an embeddings table + DuckDB's vector-similarity-search
  extension, queried via `ev sql`) — not something to work around by
  guessing matches.
- **A date inside the content text is not a schedule.** `ev find` can still
  match the words, but `ev schedule`/`--overdue` can't see it — whenever
  the user gives a real date/time, add a time mark with `ev when`. And
  since an event carries only *one* effective time, a range (trip,
  sabbatical) is one event per endpoint — a second `ev when` on the same
  event would silently supersede the first as its effective time.
