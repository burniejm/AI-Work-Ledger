# Removing Work Ledger

> Uninstalling with data removal also deletes the marker-tagged
> `post-commit` line Work Ledger added to any repository via
> `worklog githooks install` (tracked exactly; other hook content is
> untouched). To remove just the git hooks, run `worklog githooks remove
> --all-mapped` at any time.

Removal has two independent parts: the **plugin** (code and hooks) and
**your data** (`~/.claude/work-ledger/`). Removing the plugin does not
touch your data — that's deliberate, so an accidental or exploratory
uninstall never destroys your work history.

## 1. Remove the plugin

```
/plugin uninstall work-ledger
```

This removes Work Ledger's code and unregisters its seven hooks and the
`worklog` skill from Claude Code. Your data at `~/.claude/work-ledger/` is
**deliberately retained** — nothing here deletes it.

## 2. If your history matters, export it first

Before deleting any data, consider exporting it:

```sh
worklog export --format json
```

(also available as `--format markdown` or `--format csv`, with `--out
<path>` to choose the destination). The export contains your full
structured history and no credentials — connection references are nulled
out, not just redacted.

## 3. Removing your data: `worklog uninstall`

`worklog uninstall` (with no flags) is **inventory only — it deletes
nothing**:

```sh
worklog uninstall
```

This prints exactly what a real deletion would remove: the data directory
path, the database file (and its size), row counts for the main tables,
counts of backups/exports/logs, and any credential *references* on file
(never the secrets themselves, which live only in the OS keychain or an
environment variable).

To actually delete your data, add `--data`:

```sh
worklog uninstall --data
```

This prints the same inventory, then requires you to type the exact
confirmation phrase:

```
delete my work ledger
```

Typing anything else — a typo, an empty line, "yes", "y" — **refuses and
deletes nothing**. There is no `--yes`/`--force` shortcut around this
prompt; it exists specifically so that deleting your entire work history
can never happen from a single flag or a fat-fingered command.

Once confirmed, `worklog uninstall --data` deletes the whole
`~/.claude/work-ledger/` directory: the database, backups, exports, logs,
and saved setup state.

### Also deleting stored credentials: `--delete-credentials`

By default, deleting your data does **not** touch anything in the OS
keychain — only the local `credential_reference` pointers disappear along
with the database. To also remove the actual keychain entries:

```sh
worklog uninstall --data --delete-credentials
```

`--delete-credentials` is honored **only** together with `--data` and the
correct typed confirmation phrase — passing it alone, or with a wrong
phrase, is a silent no-op that deletes nothing. Credential deletion is
never implicit: removing a connection or your data always leaves the
underlying secret in the keychain unless you explicitly opt in with this
flag, because a keychain entry may be shared with, or still needed by,
something outside Work Ledger.

The result reports, per credential reference, whether it was actually
removed — a missing `security` binary, a keychain entry that no longer
exists, or any other failure is reported as "not removed" with a reason,
never silently swallowed.

### Removing keychain entries by hand

If you'd rather remove a specific connection's credential yourself (with or
without also running `worklog uninstall`), each keychain entry is
addressable directly:

```sh
security delete-generic-password -s work-ledger -a <connection-name>
```

The service (`-s`) is always `work-ledger`; the account (`-a`) is the
connection's name, matching the `keychain://work-ledger/<connection-name>`
reference stored in `config.yaml`/the database. `worklog uninstall`
(without `--data`) or `worklog connections list` will show you the exact
reference for each connection if you're not sure of the name.

## Summary

| You want to... | Run |
| --- | --- |
| Remove the plugin's code and hooks, keep your data | `/plugin uninstall work-ledger` |
| See what removal would delete, without deleting anything | `worklog uninstall` |
| Save your history before deleting it | `worklog export --format json` |
| Delete your data (database, backups, exports, logs) | `worklog uninstall --data` (then type `delete my work ledger`) |
| Also delete stored Jira credentials from the keychain | `worklog uninstall --data --delete-credentials` |
| Delete one connection's keychain entry by hand | `security delete-generic-password -s work-ledger -a <connection-name>` |
