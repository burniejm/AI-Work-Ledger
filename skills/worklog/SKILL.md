---
name: worklog
description: >-
  Query, record, and correct the local Work Ledger — the automatic history of work performed in Claude Code
  sessions, organized by client, project, and Jira or local work item. USE THIS SKILL whenever the user asks
  what they worked on ("what did I work on yesterday", "show my work for August 29", "what did I do this week"),
  asks for status ("give me the status of Acme", "what remains on AB-76", "what is blocked across all
  clients", "what have I not touched in 14 days"), wants to record work done outside Claude
  ("record that I spent an hour testing AB-326 last night", "/worklog add ..."), wants to correct the ledger
  ("move yesterday's activity from AB-326 to AB-76", "Portal belongs to Globex",
  "that remaining item was completed Friday", "delete that entry"), asks to see evidence for an entry,
  asks about unresolved inferences, or runs any /worklog subcommand (setup, today, day, range, status,
  remaining, blocked, stale, evidence, add, correct, inferences, connections, backup, export, import, doctor).
argument-hint: "[subcommand] [args]  e.g. today | status Acme | add <description> | correct <description>"
allowed-tools: Bash(worklog:*), Bash("${CLAUDE_PLUGIN_ROOT}"/bin/worklog:*), Bash(*/bin/worklog:*), mcp__atlassian__getJiraIssue, mcp__atlassian__searchJiraIssuesUsingJql, mcp__atlassian__getVisibleJiraProjects, mcp__atlassian__atlassianUserInfo, mcp__atlassian__getAccessibleAtlassianResources, mcp__atlassian__getJiraIssueRemoteIssueLinks
user-invocable: true
---

# Work Ledger

Current ledger context (workday, timezone, open clients, pending contradictions, inbox size, stale connections):

!`"${CLAUDE_PLUGIN_ROOT}"/bin/worklog context --format json 2>/dev/null || true`

If that block is empty, the command could not run at all — the ledger is probably not installed or not set up.
Run `"${CLAUDE_PLUGIN_ROOT}"/bin/worklog doctor --format json` and, if it reports an uninitialized ledger, offer
`"${CLAUDE_PLUGIN_ROOT}"/bin/worklog setup`. A block that parses but shows no clients just means nothing has
been captured yet, which is a perfectly good answer on its own.

User request: $ARGUMENTS

Your job is to translate what the user said into **one canonical invocation**, run it, and render the JSON it
returns. The CLI does all filtering, grouping, workday math and confidence scoring. You render and ask
follow-up questions — nothing else.

## The one way to invoke the ledger

Always this exact form, always with `--format json`:

```
"${CLAUDE_PLUGIN_ROOT}"/bin/worklog <subcommand> [args] --format json
```

The quoted `"${CLAUDE_PLUGIN_ROOT}"` matters: the path can contain spaces, and this is the form the skill's
`allowed-tools` patterns are written against. If `${CLAUDE_PLUGIN_ROOT}` is somehow unset, the plain `worklog`
name on `PATH` is the only fallback.

**The table below writes just `worklog ...` for readability. Always run it with the full
`"${CLAUDE_PLUGIN_ROOT}"/bin/` prefix.**

## Canonical operations

| What the user said | Run exactly this |
| --- | --- |
| "what did I work on today", "/worklog today" | `worklog today --format json` |
| "what did I work on yesterday", "show my work for August 29" | `worklog day yesterday --format json` / `worklog day 2026-08-29 --format json` |
| "what did I do this week", "August 24 through August 30" | `worklog range "august 24" "august 30" --format json` |
| "give me the status of Acme", "how is AB-76 going" | `worklog status Acme --format json` / `worklog status AB-76 --format json` |
| "how much AI did we use on AB-76", "was AB-76 mostly AI-written" | `worklog status AB-76 --format json` — read `ai_usage` (score 1-5, confidence, reason); null score means "no evidence, unknown" |
| "how long did I spend on AB-76", "time on Acme today" | `worklog status AB-76 --format json` — read `time` (minutes over the report window); or `worklog day <date> --format json` — read per-item and per-client `minutes`. Session-active time only: coding outside Claude sessions is not counted |
| "what remains on AB-76", "what's left across everything" | `worklog remaining AB-76 --format json` / `worklog remaining --format json` |
| "what is blocked", "what is blocked across all clients" | `worklog blocked --format json` / `worklog blocked Acme --format json` |
| "what have I not touched in 14 days" | `worklog stale --days 14 --format json` |
| "why do you think that", "show me the evidence for X" | `worklog evidence <activity-id or AB-76 or LOCAL-0192> --format json` |
| "record that I spent an hour testing AB-326 last night" | `worklog add --format json AB-326 spent an hour testing on hardware last night` |
| "move yesterday's activity from AB-326 to AB-76" | `worklog correct move-activity --activity <id> --to-ticket AB-76 --reason "<user's words>" --format json` |
| "Portal belongs to Globex" (learn the rule too) | `worklog correct move-activity --activity <id> --client Globex --reason "<...>" --learn-alias --format json` |
| "that remaining item was completed Friday" | `worklog correct set --table remaining_items --id <id> --field state --value completed --reason "<...>" --format json` |
| "delete that entry" | `worklog correct delete --table activities --id <id> --reason "<...>" --format json` |
| "undo that" | `worklog correct undo --group <correction_group_id> --format json` |
| "what changed on this entry" | `worklog correct history --table activities --id <id> --format json` |
| "what are you unsure about", "show the inbox" | `worklog inferences --format json` |
| "what conflicts with Jira" | `worklog contradictions --format json` |
| "what Jira connections do I have" | `worklog connections list --format json` |
| "back up the ledger" / "list backups" | `worklog backup --label "<why>" --format json` / `worklog backup --list --format json` |
| "export my history" | `worklog export --format markdown` (or `json` / `csv`; `--out <path>`) |
| "import this export" | `worklog import <path> --format json` (offer `--dry-run` first) |
| "is the ledger healthy", "something looks wrong" | `worklog doctor --format json` |
| "set up the work ledger" | `worklog setup` (interactive — hand the terminal to the user) |
| "what did the hooks actually capture" | `worklog sessions --format json` |

Notes on arguments:

- **Day expressions** are passed through verbatim: `today`, `yesterday`, `2026-08-29`, `august 29`, `aug 29 2026`.
  Do not convert them yourself — the CLI owns workday boundaries (the workday runs from the rollover hour, so
  work at 01:00 still belongs to the previous day).
- **Scopes** are a client name, a project name, or a ticket key. Pass the user's word; the CLI resolves it and
  tells you when it is ambiguous.
- **`add` is the one exception to flag placement.** Everything after the subcommand is the statement text, so
  `--format json` must come **before** it: `worklog add --format json AB-326 spent an hour on hardware`. A
  trailing `--format json` is silently recorded as part of the description instead. Optional `client=` and
  `project=` prefixes and a ticket key may appear anywhere in the statement; quote nothing that does not need
  quoting.
- **Ids** (`--activity`, `--id`, `--group`) always come from a previous `--format json` result. Never invent one.
  If you do not have the id the user is pointing at, run the relevant report first and identify the row.

## Never do these

- Never open the ledger database directly, never run `sqlite3`, never construct or execute a SQL string, and
  never edit a row by hand. Every read and every write goes through a `worklog` subcommand.
- Never call a Jira write tool — no create, edit, transition, comment, or work-log tool, ever, for any reason.
  Jira is read-only here; only the six read tools named in `allowed-tools` may be called.
- Never run a git command that changes anything, never commit, never push. The ledger records work; it does not
  perform it.
- Never invent an activity id, a correction group id, a ticket key, or a number. If a value is not in the JSON
  you just read, say you do not have it.
- Never present an inferred elapsed time as a fact. The ledger records when work happened, not a stopwatch;
  say "roughly" and name it as an estimate, or omit it.

## How to render the answer

1. **Group** the result: client → project → work item. Keep that order even when only one group exists.
2. **Summarize by default.** One line per work item, plus what changed. Do not list files, commits, or
   transcript excerpts unless the user asked for detail — then re-run the report with `--evidence`, or run
   `worklog evidence <entry> --format json` for one entry.
3. **Mark inference.** Anything with `confidence: "medium"` — the CLI also sets `"inferred": true` on it — is
   inferred, not observed, so say so ("looks like", "inferred from the branch name"). `high` can be stated
   plainly. `low` never appears in a normal report; it lives in `worklog inferences` and is only mentioned if
   the user asks what you are unsure about.
4. **Always surface contradictions.** If the JSON carries contradictions, or the preamble reports
   `pending_contradictions > 0` for the scope you are reporting on, mention them: what the ledger says, what
   Jira says, and that `worklog contradictions --format json` lists them in full.
5. **Always surface Jira staleness.** If a report carries a staleness note, or a connection appears in
   `stale_connections`, say when the Jira data was last fetched and that it may be out of date.
6. **Empty is an answer.** "Nothing recorded for that day" is correct and complete — never pad it with guesses.

## Refreshing Jira before a status answer

Jira data reaches the ledger through you: a CLI process has no channel to an MCP tool, so you are the
transport. Before answering a *status*, *remaining*, or *blocked* question that touches an MCP-backed
connection:

1. Run `"${CLAUDE_PLUGIN_ROOT}"/bin/worklog jira plan-refresh --format json`. It returns
   `{"connections": [{"connection", "mcp_server", "tool", "cloud_id", "issue_keys": [...]}]}`. An empty list
   means nothing is stale — skip straight to the answer.
2. For each entry, call the named read tool — `mcp__<mcp_server>__<tool>`, e.g.
   `mcp__atlassian__getJiraIssue` — once per issue key, passing `cloud_id` as the cloud id. The plan is already
   bounded by `jira.max_refresh_per_turn`; do not fetch keys it did not name, and do not exceed the keys it
   listed.
3. Pipe the collected results back in, as JSON on stdin:

   ```
   "${CLAUDE_PLUGIN_ROOT}"/bin/worklog jira ingest --connection <name> --format json
   ```

   Write the payload to a temp file and redirect it in. The CLI treats stdin as untrusted data: it validates
   shapes, redacts, ignores unknown fields, and writes only through parameterized statements. It answers with
   `{"updated", "rejected", "unknown_keys", "contradictions_opened"}`.
4. Then run the report and answer.

**If any of this fails — the tool is not available, the server is not connected, the call errors, `ingest`
rejects the payload — answer the question anyway from what the ledger already holds, and state plainly that
the Jira data is stale and when it was last fetched.** A refresh failure never blocks an answer.

## MCP tool names

`allowed-tools` supports no wildcard over MCP tool names, so each read-only Atlassian tool is listed by its
exact name: `mcp__atlassian__getJiraIssue`, `mcp__atlassian__searchJiraIssuesUsingJql`,
`mcp__atlassian__getVisibleJiraProjects`, `mcp__atlassian__atlassianUserInfo`,
`mcp__atlassian__getAccessibleAtlassianResources`, `mcp__atlassian__getJiraIssueRemoteIssueLinks`. No write
tool appears in that list, which is what makes the read-only guarantee structural rather than advisory.

Those names assume the conventional MCP **server name** `atlassian`. A user whose Atlassian MCP server is
registered under a different name (say `jira-cloud`) must add the equivalent `mcp__<server>__<tool>` entries to
this skill's `allowed-tools` — `docs/INSTALL.md` explains how. Until they do, `plan-refresh` will name a tool
you are not permitted to call; that is not an error to report as a failure, it is exactly the degraded path
above: answer from the ledger and say the Jira data is stale.

## Corrections

A correction is a real, recorded, reversible edit — never a silent one.

1. Run the correction from the table above with a `--reason` that quotes what the user actually said.
2. The result carries a `correction_group_id`. **Report it**, and tell the user they can say **"undo that"** to
   reverse the whole group. Behind that phrase is `worklog correct undo --group <id> --format json`.
3. When the correction encodes a durable rule ("Portal belongs to Globex", "anything in this repo is
   Acme"), add `--learn-alias` so the same mistake is not repeated, and say that you did.
4. Corrections are append-only: the original observation is never destroyed, only superseded. Say so if the
   user seems worried about losing history.
