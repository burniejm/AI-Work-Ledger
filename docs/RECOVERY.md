# Recovering Work Ledger

## Start here: `worklog doctor`

```sh
worklog doctor                 # human-readable
worklog doctor --format json   # machine-readable
```

`doctor` runs a fixed set of checks and rolls them up into one overall
verdict: `healthy` (every check `ok`), `degraded` (at least one `warn`, none
`fail`), or `unhealthy` (at least one `fail`). A fresh or partially-set-up
install is expected to read as `degraded`, not `unhealthy` — an absent
optional subsystem (no Jira connection yet, hooks not installed yet, no
transcripts written yet) always reports `warn`, never `fail`.

Reading guide, check by check, in the order `doctor` prints them:

| Check | What `ok` means | What to do if it isn't |
| --- | --- | --- |
| `data_dir` | `~/.claude/work-ledger/` (or `$WORK_LEDGER_HOME`) exists. | `warn` just means nothing has run yet — any `worklog` command creates it. |
| `permissions` | The data directory and its files are `0700`/`0600` — private to your user. | `warn` names the offending path and its actual mode; `chmod` it back, or just let a future write re-create it correctly. |
| `synced_filesystem` | The data directory is not inside Dropbox, iCloud Drive, Google Drive, OneDrive, or a network mount. | `fail` here is serious — see "Synced filesystems" below. Move the directory (or set `WORK_LEDGER_HOME`) off the synced location. |
| `config` | `config.yaml` parses and passes validation. | `fail` lists the specific problem(s); fix `config.yaml` by hand or delete it to regenerate defaults. |
| `database_writable` | A write lock (`BEGIN IMMEDIATE`) can be acquired. | `fail` usually means another process is holding a long write lock, or the file/directory isn't writable. |
| `schema_version` | The applied schema matches (or is behind, with pending migrations pending) what this build supports. | `warn` names pending migrations — run `worklog migrate`. `fail` means a `NewerDatabase` or checksum problem; see `docs/UPGRADE.md`. |
| `integrity` | `PRAGMA integrity_check` returns `ok`. | `fail` means real corruption — restore from a backup (below). |
| `wal_mode` | The database is in WAL journal mode. | `warn` if it isn't — this should self-correct on the next connection; if it doesn't, the file may be on an unsupported filesystem. |
| `hooks_installed` | `hooks/hooks.json` exists, parses, and is non-empty. | See "When hooks stop firing" below. |
| `recent_capture` | At least one `source_events` row was written in the last 7 days (or hooks aren't installed yet, which is `ok` on its own). | `warn` with hooks installed but nothing captured recently — see "When hooks stop firing". |
| `checkpoint_failures` | No recent `capture_checkpoints` row has `status='failed'`. | `warn` lists the failing hook event and its detail — check `logs/hooks.log` for the full context. |
| `transcripts_readable` | Claude Code's project transcript directory is readable. | `fail` means a permissions problem on that directory; `warn` means it doesn't exist yet (nothing captured so far). |
| `git_identity` | At least one Git identity is configured (from setup). | `warn` (not-yet-configured) until you run `/worklog setup`; work by unconfigured Git identities is stored as evidence but never described as yours. |
| `connections` | Every enabled Jira connection is `healthy`. | `warn` names the degraded ones (`name=state`) — run `worklog connections test <name>` or `worklog connections repair <name>`. A Jira problem never blocks local capture. |
| `stage_b` | The background LLM summarizer is enabled and `claude` is on `PATH` (or it's deliberately disabled — also `ok`). | `warn` explains why (not on `PATH`, disabled) or reports the last run's outcome if it wasn't clean. Stage A results stand on their own either way. |
| `subagent_capture` | At least one sidechain event has been captured. | `warn` ("unverified") is the expected steady state on many setups — subagent transcript capture is best-effort by design (DESIGN gap G4), not a fault. |
| `backups` | At least one backup exists in `backups/`. | `warn` if none yet — run `worklog backup` periodically. |

## Restoring from a backup

List and take backups:

```sh
worklog backup --list           # every backup found, oldest first
worklog backup --label "before cleanup"
```

Backups are full SQLite snapshots taken through the backup API (never a raw
file copy), stored under `backups/`, and contain no credentials — a
connection's `credential_reference` (a `keychain://` or `env://` string)
survives, but the secret it points to never does.

To restore:

```sh
worklog restore <path> --confirm
```

`--confirm` is required — this replaces the live database. A safety backup
of whatever's currently live is taken automatically first (labeled
`pre-restore`), so a restore is itself always undoable by restoring that
safety snapshot.

## Rebuilding interpretation from raw evidence

If the ledger's summaries or work-item attributions look wrong for a
particular session — a bad Stage A/Stage B run, a session that was
interrupted mid-interpretation — you don't need to touch raw data to fix
it. `worklog reprocess` re-derives everything for that session from its
raw evidence (the captured `source_events` and any reconciled transcript
and git records), which are **never destroyed** by a correction:

```sh
worklog sessions                              # find the session id
worklog reprocess <session-id> --stage both   # or --stage a / --stage b
```

This is safe to run repeatedly: it creates no duplicate events and no
duplicate activities — every write is keyed by an idempotency derived from
the evidence itself, not from wall-clock time. If you've since made manual
corrections (`worklog correct ...`) on top of what Stage A/B produced, note
that reprocessing regenerates the underlying activity but does not remove
your corrections — check `worklog correct history` afterward if you want to
confirm the two agree. A `correct move-activity` re-attribution (including
`--learn-alias`) also survives reprocessing without re-materializing a
duplicate under the old attribution: it rewrites the activity's own
idempotency key to match its corrected attribution as part of the same
correction, so a later Stage A run over the same session updates that row
instead of inserting a new one.

## Recovering a degraded or expired Jira connection

```sh
worklog connections list                 # every connection and its health_state
worklog connections test <name>          # re-run validation, update health_state
worklog connections repair <name> [--credential-reference <ref>]
```

`connections test` re-runs the same validation setup does (authentication,
current-user lookup, issue retrieval for REST; recorded capabilities for
MCP) without touching any ledger history. `connections repair` does the
same, optionally after pointing the connection at a different credential
reference — useful when a token was rotated and you stored the new one
under a new keychain entry or environment variable. Neither ever deletes an
`activities`, `remaining_items`, or `contradictions` row; a Jira outage or
expired token degrades that connection's health, never your local history.

## When hooks stop firing

Symptoms: `worklog doctor`'s `hooks_installed` or `recent_capture` checks
go from `ok` to `warn`, or a session you know had activity in it produced
no new `source_events`.

1. **Verify `hooks_installed`.** `worklog doctor --format json` reports
   whether `hooks/hooks.json` exists and parses. If it's missing, the
   plugin install may be broken or was removed (`/plugin uninstall` removes
   hooks — see `docs/REMOVAL.md`).
2. **Re-install the plugin.** `/plugin uninstall work-ledger` followed by
   `/plugin install work-ledger@work-ledger` (or reinstall from your
   marketplace) re-registers the seven hook events cleanly. Your data is
   untouched either way — hooks and data are independent.
3. **Check `logs/hooks.log`.** Every hook wraps its body in a top-level
   exception handler that logs and returns 0 rather than failing loudly
   toward Claude Code, so a hook that's silently misbehaving leaves a trail
   here (redacted, rotated at 5 x 1 MB) even though your session never saw
   an error.
4. Run `worklog sessions` to see what has actually been captured recently,
   and cross-reference against `capture_checkpoints` failures surfaced by
   the `checkpoint_failures` doctor check.

## Synced filesystems: a corruption risk, not a convenience

Never place `~/.claude/work-ledger` inside Dropbox, iCloud Drive (Mobile
Documents), Google Drive, OneDrive, or a network-mounted filesystem. The
database is SQLite in WAL mode, which depends on file locking and atomic
renames that these sync layers do not reliably provide; syncing a live WAL
database risks silent corruption, not just a sync conflict that shows up as
two files. `worklog doctor`'s `synced_filesystem` check fails hard if it
detects one of these locations or a known network filesystem type
(NFS/SMB/AFP/WebDAV and similar). If `integrity` also fails at the same
time, treat it as real corruption and restore from the most recent backup
rather than trying to repair the live file in place.

## What protects this data, and what doesn't

The database is protected by **filesystem permissions only** — the data
directory is `0700` and its files are `0600`, private to your user account.
**There is no encryption at rest.** Anyone with read access to your user
account's files (root, a backup tool with elevated access, another process
running as you) can read the raw SQLite file. This is a deliberate, stated
limitation, not an oversight: a hostile local user with your own
credentials, and encryption at rest beyond filesystem permissions, are both
explicitly out of scope for v1. If that threat model matters to you, keep
`~/.claude/work-ledger` on an already-encrypted volume (e.g. FileVault) —
Work Ledger does nothing to prevent that, and does nothing on its own to
provide it either.
