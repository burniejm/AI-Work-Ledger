# Upgrading Work Ledger

## Updating the plugin

```
/plugin update work-ledger
```

This updates the plugin's code and hooks. It does not touch your data —
your ledger stays exactly where it is, at `~/.claude/work-ledger/`.

## Schema migrations run automatically

Work Ledger's database schema evolves through a small, ordered set of
migration files. Migrations are **strictly forward-only** and
**additive**: nothing in this plan ever ships a migration that drops a
column or renames a table out from under old data.

You do not need to run anything by hand — `migrations.ensure_current()`
applies any pending migration automatically the next time it's called, and
it's called from several places that fire naturally in ordinary use:

- **Every Claude Code hook** (`session-start`, `stop`, and the rest) —
  which means simply starting a new Claude Code session after an update is
  usually enough.
- `worklog setup`
- `worklog doctor`

`worklog migrate` also runs migrations directly, and is the most explicit
way to bring the schema up to date without starting a Claude Code session:

```sh
worklog migrate            # applies any pending migrations, or prints "up to date"
worklog migrate --status   # lists every known migration and whether it's applied
```

Each applied migration's checksum is recorded and re-verified on every run,
so a corrupted or hand-edited migration file is caught rather than silently
re-applied.

### Recommended: back up before you update

Work Ledger does **not** currently take an automatic backup before running
a migration. Before updating the plugin, it's worth taking one yourself:

```sh
worklog backup --label pre-upgrade
```

This is cheap (a single SQLite backup-API snapshot into
`backups/`), and gives you something to restore from with `worklog restore`
(see `docs/RECOVERY.md`) if anything about the new schema surprises you.

## "Database written by a newer Work Ledger"

If you ever downgrade — reinstall an older version of the plugin over a
database that a newer version already migrated — `worklog migrate`,
`worklog doctor`, and every hook will refuse to touch the database rather
than risk corrupting it. You'll see an error like:

```
database has migration 5 applied; this build only knows migrations up to 4
```

This is the `NewerDatabase` protection: the code compares the highest
migration version recorded in the database against the highest version it
knows how to apply, and refuses outright if the database is ahead. Nothing
is deleted or modified when this happens — the database is left exactly as
it was.

To recover:

- **Preferred:** reinstall the newer version of Work Ledger that originally
  wrote that schema version, so the code and the database agree again.
- **Alternative:** restore an older backup taken before the newer schema
  was applied (`worklog backup --list`, then `worklog restore <path>
  --confirm` — see `docs/RECOVERY.md`). This loses any activity recorded
  between that backup and now.

## Checking your current state

```sh
worklog doctor            # overall health, including the schema_version check
worklog migrate --status  # every migration Work Ledger knows about, applied or pending
worklog version           # installed plugin version and the schema version it supports
```

`worklog doctor`'s `schema_version` check reports `ok` with the applied
version when everything is current, `warn` naming any pending migrations,
or `fail` if it hits a `NewerDatabase` or checksum-mismatch condition —
never a silent success.
