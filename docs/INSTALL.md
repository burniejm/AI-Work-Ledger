# Installing Work Ledger

## Prerequisites

- macOS (v1 targets macOS only — see "Other platforms" below).
- Python 3.10 or newer (`python3 --version`). Work Ledger is stdlib-only —
  nothing to `pip install`.
- `git` on `PATH` (used read-only, for repository and commit discovery).
- Claude Code 2.1 or newer. Verified against Claude Code 2.1.251–2.1.252;
  no plugin-loading differences were observed across that range.

## Install

The repository itself is both the plugin and a single-plugin local
marketplace — it ships `.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json`, so no separate marketplace repository is
needed.

From inside a Claude Code session:

```
/plugin marketplace add ~/Documents/code/workledger
/plugin install work-ledger@work-ledger
```

This is the exact path verified during development, using the equivalent
non-interactive CLI form:

```sh
claude plugin marketplace add "$PWD"
claude plugin install work-ledger@work-ledger
claude plugin list          # confirms work-ledger@work-ledger, enabled
```

Point the marketplace `add` at wherever you cloned the repository (adjust
the path if it isn't `~/Documents/code/workledger`).

**Development install.** If you're working on Work Ledger itself rather
than installing it as an end user, run Claude Code with
`claude --plugin-dir ~/Documents/code/workledger` (or `--plugin-dir "$PWD"`
from the repo root) instead of the marketplace/install steps above — this
loads the plugin directly from the checkout with no install step.

Either path registers seven hooks and the `worklog` skill (see "Hooks" and
"Skill" below).

### Other platforms

Windows and Linux are not implemented in v1: the credential store
abstraction has real implementations only for the macOS Keychain (via
`/usr/bin/security`). On those platforms, credential storage falls back to
`env://` references with a printed warning during setup, but installation
itself has not been verified there.

## Optional: git-side capture

`worklog githooks install --repo <path>` (or `--all-mapped`) records your
manual commits at commit time via a `post-commit` hook line, sharpening
the AI-usage score's human side. Strictly opt-in — the setup wizard's
`githooks` step installs nothing unless you say yes. See README
"Git-side capture".

## First run: `/worklog setup`

Run the setup wizard once, from inside Claude Code (`/worklog setup`) or
directly (`worklog setup` / `bin/worklog setup`). It is resumable —
progress is tracked in `state/setup.json`, and re-running it never
duplicates a connection, identity, or repository.

The wizard walks through:

1. Directories, configuration, database, and migrations.
2. Timezone, workday rollover, and evidence preferences.
3. Claude transcript locations.
4. Jira connection detection and validation (see below).
5. Current Jira user identity, per connection.
6. Git author identity (`user.name`, `user.email`, and any additional
   author emails you confirm belong to you).
7. Repository discovery, limited to user-approved locations and recent
   Claude project directories — it never scans the whole filesystem.
8. Hook installation and validation.
9. An end-to-end capture test.
10. A completion and health report.

The completion report looks like this (from the spec, and matching the
wizard's actual output shape):

```text
Work Ledger setup complete

Database: healthy
Claude capture hooks: healthy
Git identity: configured
Repositories discovered: 12
Jira connections:
  Acme Jira: healthy via MCP
  Globex Jira: healthy via MCP
Reporting timezone: America/Chicago
Workday rollover: 3:00 AM

Unresolved repository mappings: 2
```

Non-interactive setup is also supported, for scripting or CI-style
installs: `worklog setup --non-interactive` runs with defaults and prompts
for nothing, and `worklog setup --answers answers.json` supplies every
answer from a JSON file. `worklog setup --only STEP` re-runs a single step
(useful after fixing one thing without repeating the whole wizard), and
`worklog setup --reset` clears saved progress and starts over.

## Adding a Jira connection

Work Ledger prefers, in order: an existing authenticated Jira MCP
connection, Jira MCP via OAuth, a Jira Cloud REST API token, then a
personal access token for a compatible self-managed instance. Whichever
method you use, **Jira access is read-only** — no code path in this plugin
can write to Jira.

### Option A: MCP (recommended if you already have an Atlassian MCP server)

If Claude Code already has an Atlassian MCP server configured, `/worklog
setup` (or `worklog jira discover`) detects it, tests current-user lookup
and issue retrieval, and records the server name, site identifier, and
capabilities it discovered — never the MCP server's own credentials, which
Work Ledger never reads or copies.

```
worklog jira discover --format json
```

lists configured MCP servers and flags which ones look Jira-capable.

### Option B: Direct REST connection

If MCP isn't available (or you want hook-time refresh alongside it — see
below), give the wizard your Jira Cloud site URL, account email, and an API
token (or a personal access token for self-managed Jira). The token is
stored in the OS keychain — **never** in SQLite or `config.yaml`, which
hold only a reference:

```yaml
adapter: jira-rest
base_url: https://example.atlassian.net
username: user@example.com
credential_reference: keychain://work-ledger/acme-jira
```

To store or inspect that keychain entry yourself:

```sh
# store (or update) the token
security add-generic-password -U -a acme-jira -s work-ledger -w

# confirm it's there (prints the token — be careful where you run this)
security find-generic-password -a acme-jira -s work-ledger -w
```

The account (`-a`) is the connection's name; the service (`-s`) is always
`work-ledger`. When entering a token interactively, `security` prompts for
it directly rather than accepting it as a command-line argument — that
keeps the secret out of your shell history and `ps` output.

### Recommendation: run both if you want hook-time refresh

Hooks can only refresh a **REST** connection automatically (at session
start and during a session) — there is no mechanism for a hook to call an
MCP tool ad hoc; only the model, mid-conversation, can do that. An
MCP-only connection is refreshed the next time you use the `worklog` skill
(the plan/ingest loop described in `docs/DESIGN.md` §4.5 and gap G2), and
until then reports are marked with how stale the Jira data is. If you want
tickets refreshed automatically between conversations, configure a REST
connection alongside your MCP one, even if you primarily use MCP for
richer queries.

### Using a different MCP server name

The `worklog` skill's frontmatter (`skills/worklog/SKILL.md`) lists the
exact MCP tools it's allowed to call:

```
mcp__atlassian__getJiraIssue
mcp__atlassian__searchJiraIssuesUsingJql
mcp__atlassian__getVisibleJiraProjects
mcp__atlassian__atlassianUserInfo
mcp__atlassian__getAccessibleAtlassianResources
mcp__atlassian__getJiraIssueRemoteIssueLinks
```

Claude Code's `allowed-tools` frontmatter has **no wildcard over MCP tool
names** — each `mcp__<server-name>__<tool>` has to be listed exactly, by
its full name, including the server name in the middle. If your Atlassian
MCP server is registered under a name other than `atlassian` (for example
`jira` or `atlassian-work`), the skill's six `mcp__atlassian__*` entries
won't match your server's tools at all, and the model won't be able to call
them.

To fix this, edit the `allowed-tools:` line in `skills/worklog/SKILL.md`
and replace `atlassian` with your server's actual name in all six entries,
e.g. `mcp__jira__getJiraIssue`, `mcp__jira__searchJiraIssuesUsingJql`, and
so on. `worklog jira discover --format json` prints the configured MCP
server names if you're not sure what yours is called.

## Hooks

Work Ledger registers seven hook events, each calling
`"${CLAUDE_PLUGIN_ROOT}"/bin/worklog hook <event>` directly (no shim
script):

| Claude Code event | worklog hook event |
| --- | --- |
| `SessionStart` | `session-start` |
| `UserPromptSubmit` | `user-prompt-submit` |
| `PostToolUse` | `post-tool-use` |
| `Stop` | `stop` |
| `SubagentStop` | `subagent-stop` |
| `PreCompact` | `pre-compact` |
| `SessionEnd` | `session-end` |

All seven were confirmed to load and fire with no registration errors or
warnings against installed Claude Code 2.1.252. `PostToolUseFailure` and
`PostCompact` are **deliberately not registered** in v1 — nothing in
`hooks/hooks.json` produces those events, and no capability in this plan
depends on them.

Every hook is designed to return quickly (typically well under 200 ms),
never blocks your session, and never exits non-zero toward Claude Code —
a hook failure is logged to `logs/hooks.log` and swallowed, not surfaced as
an error in your conversation.

## Skill

The natural-language interface is the `worklog` skill, invoked as
`/work-ledger:worklog` (plugin name `work-ledger`, skill name `worklog`) —
**not** `/worklog:worklog`. You can also just ask in plain language
("what did I work on yesterday", "give me the status of Acme") and
Claude Code will route it to the skill based on its description.

## Warning: never put `~/.claude/work-ledger` on a synced filesystem

Do not let `~/.claude/work-ledger` live inside Dropbox, iCloud Drive
(Mobile Documents), Google Drive, OneDrive, or any network-mounted
filesystem (NFS, SMB, AFP, WebDAV, and similar). Work Ledger's database is
SQLite in WAL mode, which depends on file locking and atomic renames that
these sync layers do not reliably provide — writing to a synced folder
risks silent, hard-to-detect database corruption, not just a sync conflict.
`worklog doctor` actively checks for this (the `synced_filesystem` check)
and reports a hard failure if it detects one of these locations or a
network filesystem type. If you need the data available on another
machine, use `worklog export` and copy the export file yourself; Work
Ledger does not support multi-device sync in v1.
