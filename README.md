# Work Ledger

Work Ledger is a Claude Code plugin that automatically captures the work you do
inside Claude Code sessions — what you touched, which ticket it belongs to,
what you said you were doing — into a local, queryable history. It runs on
your machine, writes to a single SQLite file under `~/.claude/work-ledger/`,
and never leaves that machine.

Ask it things like "what did I work on yesterday", "give me the status of
Acme", "what's blocked on AB-76", or "what have I not touched in two
weeks", either from the `/work-ledger:worklog` skill inside Claude Code or
directly from the `worklog` command line.

## The hierarchy

Everything Work Ledger records is organized into one shape:

```
client -> project -> work item -> activity
```

- **Client** — who the work is for (a company, a team, or just "personal").
- **Project** — a grouping under a client (optional; many work items sit
  directly under a client with no project).
- **Work item** — a unit of work: either a Jira ticket (`kind='jira'`,
  identified by its external key like `AB-76`) or a purely local item
  (`kind='local'`, identified by a display id like `LOCAL-0192`).
- **Activity** — one observed or stated unit of progress against a work
  item, with a confidence bucket (`high`/`medium`/`low`/`contradictory`) and
  evidence backing it.

Jira is authoritative for ticket key, title, status, resolution, assignee,
and other official fields. Work Ledger is authoritative for the detailed
work history and technical progress it observes — it never edits Jira.

## Quick start

```
/plugin marketplace add ~/Documents/code/workledger
/plugin install work-ledger@work-ledger
/worklog setup
/worklog today
```

`/worklog setup` walks you through timezone, Git identity, repository
discovery, and an optional Jira connection (see `docs/INSTALL.md`). After
that, Claude Code's hooks capture activity automatically every session — you
don't have to do anything else to keep the ledger current.

## Command surface

Every subcommand accepts `--format text` (default) or `--format json`. Run
`bin/worklog <command> --help` (or `worklog <command> --help` once installed)
for the full flag list.

| Command | What it does |
| --- | --- |
| `worklog setup [--reset] [--only STEP] [--non-interactive --answers FILE]` | Resumable setup wizard: directories, preferences, transcripts, Jira, Git identity, repository discovery, hooks, an end-to-end check, and a completion report. |
| `worklog today` | Today's workday report. |
| `worklog day <expr>` | A report for one workday (`today`, `yesterday`, `2026-08-29`, `august 29`). |
| `worklog range <start> <end>` | A report spanning a range of workdays. |
| `worklog status [scope]` | Recent status for a client, project, or work item (or everything). Ticket scopes include the AI-usage score (below). |
| `worklog timesheet <client> --from <day> --to <day>` | Client-facing timesheet: one row per workday and ticket with merged minutes. `--format md\|csv\|json\|text`, `--round <minutes>` rounds rows up to billing increments, `--internal` adds AI-usage fields (excluded by default). |
| `worklog week [client] [--ending <day>]` | Monday-to-date digest per client and ticket with merged minutes; `--format md`, `--internal` for AI/cost fields. |
| `worklog standup [client]` | Yesterday / Today / Blockers, terse. |
| `worklog githooks install\|remove\|status [--repo PATH \| --all-mapped]` | Opt-in git-side capture: a marker-tagged `post-commit` hook line that records human commits the moment they happen (see below). |
| `worklog remaining [scope] [--state STATE]` | Open remaining items, optionally filtered by scope or state. |
| `worklog blocked [scope]` | Remaining items currently marked blocked. |
| `worklog stale [--days N]` | Work items with no activity in N days (default from config). |
| `worklog evidence <entry>` | The evidence chain behind one activity, remaining item, or ticket/LOCAL key. |
| `worklog add [text]` | Record work stated by hand (`worklog add AB-326 spent an hour testing on hardware last night`). Reads stdin if no text is given. |
| `worklog correct move-activity\|set\|delete\|undo\|history` | Correct the ledger — move an activity, set a field, soft-delete a record, undo a prior correction group, or view a record's correction history. Corrections are append-only; undo is derived, never destructive. |
| `worklog context` | Cheap orientation preamble (today's workday/timezone, open clients, pending contradictions, inbox size, stale connections) — this is what the `worklog` skill runs inline before every invocation; safe and fast enough to run yourself with `--format json`. |
| `worklog inferences [--limit N]` | The inbox of low-confidence inferences awaiting review. |
| `worklog contradictions [scope] [--resolve ID --note TEXT]` | Open contradictions between the ledger and Jira, or resolve one locally. |
| `worklog connections list\|test\|repair\|remove [name]` | Manage Jira connections and their health state. |
| `worklog jira discover\|plan-refresh\|ingest\|issues\|refresh` | The Jira read loop (MCP-mediated and REST). |
| `worklog backup [--label TEXT] [--list]` | Take or list on-disk backups of the ledger. |
| `worklog restore <path> --confirm` | Replace the live database with a backup (a safety backup of the current database is taken first). |
| `worklog export --format json\|markdown\|csv [--out PATH]` | Export the ledger's history. Contains no credentials. |
| `worklog import <path> [--dry-run]` | Import a previously exported JSON file. |
| `worklog reprocess <session> [--stage a\|b\|both]` | Re-run interpretation for one session from its raw evidence. |
| `worklog sessions [--limit N]` | Recent capture sessions and what the hooks recorded for each. |
| `worklog doctor` | A full health check: data directory, permissions, schema, hooks, connections, Stage B, backups, and more. |
| `worklog migrate [--status]` | Apply pending schema migrations, or just report status. |
| `worklog config show\|path\|validate` | Inspect the non-secret configuration. |
| `worklog uninstall [--data] [--delete-credentials]` | Inventory (default) or, with `--data` and a typed confirmation, delete the ledger's data directory. See `docs/REMOVAL.md`. |
| `worklog version` | Print the installed version and schema version. |

`worklog hook <event>` also exists, but it is an internal entry point invoked
by `hooks/hooks.json` — you should never need to run it by hand.
`worklog stage-b [--session ID] [--force]` is similarly internal/background:
it's what the `Stop` hook spawns to run the background LLM summarizer for one
session, and what `worklog doctor`'s `stage_b` check reports on — you can run
it by hand against a specific session id for debugging, but it isn't part of
normal day-to-day use.

## What Work Ledger does not do

Directly from the spec's non-goals for version one:

- Billing-grade time tracking
- Employee surveillance or productivity scoring
- Multi-user or team access
- Multiple-computer synchronization
- Claude Desktop or Cowork support
- Hosted dashboards
- Automatic Jira comments, worklogs, edits, or transitions
- Automatic Git commits or pushes
- Continuous crawling of every Jira project
- Copying entire Claude transcripts or source files into the database
- Replacing Jira as the official ticket workflow system

## Privacy

- **Single-device, local only.** Everything lives in
  `~/.claude/work-ledger/` on the machine where you run Claude Code. There
  is no server, no sync, and no telemetry — Work Ledger never sends data
  anywhere except to Jira, and only to read tickets you already have access
  to.
- **Secrets never touch the database.** Credentials live in the OS keychain
  (macOS Keychain today) or an environment variable; SQLite and
  `config.yaml` hold only a reference such as `keychain://work-ledger/<name>`
  or `env://VAR`. Secrets are redacted before anything is written to logs,
  summaries, evidence excerpts, exports, or backups.
- **Jira is strictly read-only.** No adapter in this codebase has a write
  method; Work Ledger never posts a comment, logs work, edits a field, or
  transitions a ticket.

## AI-usage score

Every Jira or local work item carries an **AI-usage score** — a 1-5 rating
of how much of its code the AI wrote, recomputed automatically as evidence
lands:

| Score | Meaning |
|---|---|
| 5 | Essentially one-shot: nearly every edit came from AI tools, minimal steering. |
| 4 | AI wrote most of it; the human steered with prompts or touch-ups. |
| 3 | Genuinely mixed: AI edits and human-only commits both carry real weight. |
| 2 | Mostly human: human-only commits dominate. |
| 1 | AI advised only: sessions were read-only; the code came from human commits. |
| — | No evidence at all (never shown as a guess). |

The number is deterministic and explainable — `worklog status AB-76` shows
it with its confidence and the counts behind it ("9 AI edits, 1 human-only
commit across 2 sessions"). The Stage B model pass may refine it by at most
one step (never more, capped at `medium` confidence), and `worklog correct`
can pin it permanently with an audit trail — a corrected score is never
recomputed over. Human-side coding is inferred from commits that show up in
git history without matching Claude session activity, so the human side is
an approximation; treat 2-vs-3 boundaries with that in mind.

## Time on ticket

Status and day reports include **session-active minutes** per work item and
per client, computed at query time from activity evidence bounds — never
stored, always current. Overlaps merge, so two activities in the same hour
count once, and a client's daily total pools its tickets' intervals so a
stretch shared by two tickets is counted once at the client level.

`worklog status AB-76` shows `time: 2h 25m in window` (the same window the
status report covers); `worklog day` shows per-item and per-client totals.
An activity with no end bound counts as `time.min_activity_minutes`
(default 5). This measures evidence-bounded *session* time, not wall-clock
effort — human coding outside Claude sessions is invisible here, the same
caveat as the AI-usage score's human side.

## Timesheets

`worklog timesheet Acme --from monday --to today` produces a paste-ready
markdown table (or CSV/JSON) of workday x ticket rows with the same merged
minutes the reports use, day subtotals, and a grand total. It is
**client-facing by default**: AI-usage fields never appear unless you pass
`--internal`. `--round 15` rounds each row up to quarter-hour billing
blocks, with totals summed from the rounded rows so the invoice adds up.

## Git-side capture (opt-in)

By default, human coding is only *inferred* from commits found at
reconcile time. `worklog githooks install --repo <path>` (or
`--all-mapped`, or the optional setup-wizard step) appends one
marker-tagged line to that repo's `.git/hooks/post-commit` so commits are
recorded the moment they happen — even with no Claude session open, which
makes them **definitive human-side evidence** for the AI-usage score.

Safety properties: it never clobbers an existing hook (one tagged line,
appended; removal deletes only that line), the capture command always
exits 0 so a ledger problem can never break `git commit`, commit subjects
pass through the same redaction as everything else, and `worklog
uninstall` removes every installed hook line. This is the only Work
Ledger feature that touches your repositories, and it is opt-in per repo.

## Claude usage & cost

Transcript reconciliation accumulates each session's token usage per model
(exactly once per record), and reports price it from a per-model rate table
— `worklog status AB-76` shows `Claude usage: 1.2M in / 85k out (~$8.13)`,
and day reports show per-client totals (each session counted once).
Unknown models show token counts with no cost — never a guessed price.
Rates ship with current Claude first-party pricing and standard cache
multipliers; override or extend them via `costs.model_rates` in
config.yaml if pricing changes:

```yaml
costs:
  model_rates:
    claude-opus-5: {input: 5.0, output: 25.0}   # USD per MTok
```

A ticket's number sums its sessions whole (same granularity as the
AI-usage score), so tickets sharing a session overlap; client/day totals
never double-count.

## Weekly digest & standup

`worklog standup` answers the daily meeting in three terse sections —
yesterday's work per ticket, today's so far, and open blockers with their
explanations. `worklog week [client]` rolls Monday-to-date into a per
client/ticket digest with merged minutes (`--format md` for a paste-ready
status email); AI-usage and cost fields appear only with `--internal`,
same rule as timesheets.

## Known limitations

Work Ledger runs inside Claude Code's hook system, which has real limits —
session-end hooks aren't guaranteed to fire, a hook can't call an MCP tool
ad hoc, active working time can't be measured honestly, and a few other
gaps like these are handled with a specific, documented degradation rather
than a silent guess. See **`docs/DESIGN.md` §14 ("Requirements that cannot
be implemented reliably")** for the full list of gaps and how each one is
handled.

## More documentation

- [`docs/INSTALL.md`](docs/INSTALL.md) — prerequisites, install, setup, and Jira connection.
- [`docs/UPGRADE.md`](docs/UPGRADE.md) — updating the plugin and its schema.
- [`docs/RECOVERY.md`](docs/RECOVERY.md) — `doctor`, backups, and recovering from problems.
- [`docs/REMOVAL.md`](docs/REMOVAL.md) — uninstalling the plugin and/or its data.
- [`docs/DESIGN.md`](docs/DESIGN.md) — architecture, data model, and design decisions.
