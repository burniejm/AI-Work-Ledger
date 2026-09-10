# Work Ledger v1 — Acceptance Criteria Verification

**Spec:** the product specification's §20 (this document maps each of its
24 acceptance criteria to the automated test, or stub, that verifies it)
**Suite:** `tests/acceptance/test_acceptance.py` (Task 39)
**Run it:** `PYTHONPATH=src python3 -m unittest discover -s tests/acceptance -v`

Every criterion below is verified by an automated test method named `test_ac<NN>_...`,
driven end to end through `hooks_runtime.handle` (the same dispatch `worklog hook <event>`
uses — Ruling 26 removed the shim layer, so there is nothing else to call) and the
`worklog` CLI (`cli.main`). No test invokes the real `claude` binary (Stage B is either
disabled by config or exercised through the stub at `tests/fixtures/bin/claude`, exactly
like `tests/test_stage_b.py`) and no test reaches a real network host (every Jira
interaction talks to a loopback `http.server` the suite starts itself).

Two criteria — 13 and 15 — are necessarily verified against a **stub** rather than a
live external system, because there is no way to drive a real Jira MCP session or a
real OS keychain deterministically and offline in an automated test. Each row below
says so plainly, and gives the manual procedure to verify the real thing.

## Criterion → test map

| # | Criterion (Spec §20) | Test method | Notes |
|---|---|---|---|
| 1 | A session touching two Jira issues produces two correctly scoped activities. | `test_ac01_two_issues_two_activities` | Two `PostToolUse` (Bash `git commit`) hook events for `AB-201`/`AB-202` in one session, `Stop` runs Stage A synchronously; asserts two `activities` rows with two distinct `work_items.external_key` values. |
| 2 | Reprocessing the same session produces no duplicate events or activities. | `test_ac02_reprocess_is_idempotent` | `worklog reprocess <session>` run three times via the CLI; `source_events`/`activities` counts are asserted stable across all three runs. |
| 3 | A session crossing midnight is reported under the correct workday using the 3:00 AM rollover. | `test_ac03_midnight_rollover` | `timeutil.utc_now_iso` is patched to drive one session through 20:00Z and 06:30Z-next-day events; `worklog day 2026-08-29` shows both tickets, `worklog day 2026-08-30` shows neither. |
| 4 | "What remains on AB-76?" combines persistent remaining work with current Jira status. | `test_ac04_remaining_with_jira_status` | A remaining item plus `worklog jira refresh` against a loopback Jira stub; `worklog status AB-76` returns both `open_work` (ledger) and `jira` (current status), matching Spec §5.4. |
| 5 | A Jira Done ticket with open ledger work produces a visible contradiction. | `test_ac05_done_ticket_contradiction` | Same shape as #4 but the stub reports `status=Done`; `worklog contradictions` lists one `jira_done_open_remaining` row. |
| 6 | Unticketed work creates or reuses an appropriate local work item. | `test_ac06_local_work_item` | Two `worklog add` statements describing the same work with different wording resolve to the same `LOCAL-NNNN` work item. |
| 7 | Repeated remaining-work mentions update or reinforce an existing item rather than blindly duplicating it. | `test_ac07_remaining_reinforced` | `worklog stage-b --force` run three times over an unchanged bundle (stub `claude`, mode `ok`); asserts exactly one `remaining_items` row with `mention_count == 3`. |
| 8 | A correction updates subsequent reports and preserves its audit history. | `test_ac08_correction_updates_reports` | `worklog add` then `worklog correct move-activity --to-ticket`; asserts the activity's summary is present under the *old* ticket's `worklog status` report **before** the correction, absent from it **after**, and present under the *new* ticket's report — a real before/after content comparison, not just a post-correction check — plus `worklog correct history` showing the audit row with its reason and correction-group id. |
| 9 | Git or Jira failure does not lose Claude-session activity. | `test_ac09_jira_outage_preserves_activity` | Two halves. **Jira:** a `jira-rest` connection pointed at a closed local port (`127.0.0.1:1`, guaranteed to refuse immediately — no real network access); a full session still lands its `session_start` event, its `git_commit_signal` event, and its Stage-A activity. **Git:** a real repository (so `Stop`'s `reconcile_git` genuinely attempts a `git log`) with `gitevidence.commits_since` mocked to raise — the one seam that forces a git failure without corrupting a real `.git` directory; asserts the session's Claude activity still lands and the `stop` checkpoint records `status='partial'` with `reconcile_git` named as the failed block. |
| 10 | Low-confidence attribution stays out of normal reports and appears in the inference inbox. | `test_ac10_low_confidence_inbox` | Driven through real hooks and Stage A, not seeded by construction: a ticket key mentioned only in tool input/output (no branch or prompt signal) scores 24 points under the real DESIGN §6 scoring — below the medium threshold — landing at `confidence='low', in_inbox=1` on its own. That activity is absent from `worklog today` (counted only in `excluded_low_confidence_count`) and present in `worklog inferences`, with the same `confidence_reason` in both places. |
| 11 | Concurrent Claude sessions do not corrupt the database or create unbounded locking. | `test_ac11_concurrent_sessions` | Reuses the Task 38 concurrency-hardening pattern (`tests/test_concurrency.py`'s `writer` shape, inlined as a top-level, picklable `_concurrent_writer`): 6 OS processes × 25 events hammer the same SQLite file; asserts `PRAGMA integrity_check` is `ok`, every row landed, and the run finishes well under a lock-unbounded timeout. |
| 12 | Full transcripts and source files are not copied into SQLite. | `test_ac12_no_bulk_copy` | A 2 MB fake transcript (`session-start`'s `transcript_path`) plus a 1 MB file write (`PostToolUse`) are captured through the real hooks; asserts the resulting database is **smaller than 2 MB**, smaller than the transcript file itself, that no `source_events.payload_bytes` exceeds `capture.payload_max_bytes`, and that the transcript's 2 MB sentinel filler string appears in no stored `payload_json`. |
| 13 | Setup detects and reuses an authenticated Jira MCP connection where available. | `test_ac13_mcp_reuse` | **STUB** — verified against a fake `~/.claude.json` (patched `pathlib.Path.home`) declaring an `atlassian` MCP server with `tools`, and `run_cli: False` (no real `claude mcp list` call). `worklog setup` creates one `adapter='mcp'` connection whose recorded capabilities make it `healthy` without a further authentication step. **Manual verification against a real MCP session:** run `worklog setup` on a machine with a real, authenticated Jira MCP server configured in Claude Code, then `worklog connections list` and confirm the discovered connection reports `health=healthy`. |
| 14 | MCP credentials are not copied into the ledger. | `test_ac14_no_mcp_credentials` | Same fake `~/.claude.json`, this time seeded with a fake `ATLASSIAN_API_TOKEN` in the server's `env` block; asserts the token never appears in the SQLite file, `config.yaml`, or any other file under `WORK_LEDGER_HOME`. |
| 15 | Direct Jira credentials are stored using an OS credential provider. | `test_ac15_credentials_in_keychain` | **STUB** — drives the real `worklog setup` path (`setupwizard._store_token`, Spec §12.4) with a raw `token` and a `keychain://` `credential_reference`, under a mocked `subprocess.run` (the code calls the absolute path `/usr/bin/security`, never resolved via `PATH`, so it cannot be redirected with a stub script on `PATH` the way `tests/test_stage_b.py` redirects `claude` — mocking the call itself is the same seam `tests/test_credentials.py`'s own `TestKeychainStore` uses). Asserts `security add-generic-password` was actually invoked once, `connections.credential_reference` in the database is the bare `keychain://...` reference, and the token itself never reaches the SQLite file. **Manual verification against a real keychain:** run `worklog setup` interactively (or with an `--answers` file carrying a raw `token`, not a pre-existing `env://` reference — see the precondition below) to configure a direct (REST) Jira connection on macOS, then run `security find-generic-password -s work-ledger -a <connection-name>` and confirm the entry exists and its account matches. |
| 16 | Multiple Jira sites can be configured independently. | `test_ac16_multiple_sites` | Two REST connections (one healthy loopback stub, one pointed at a closed port) are configured side by side and refreshed independently via `worklog jira refresh --connection <name>`; `worklog connections list` shows one `healthy` and one degraded after their own, independent refresh calls. Note: site-b's `updated == 0` in the test is a consequence of that site being unreachable, not a proof that `connection_id` scoping alone would keep a *reachable* second site's issues from ever updating the wrong site's work items — that finer-grained scoping guarantee is covered at the unit level by `tests/test_jira_refresh.py` and `tests/test_jira_mcp.py`, not re-proven here. |
| 17 | Setup identifies the Jira and Git identities belonging to the user. | `test_ac17_identities_detected` | `worklog setup` against a real local git repo (`git config user.email`) and a loopback Jira stub (`/myself`); asserts both a `kind='git'` and a `kind='jira'` row land in `user_identities`. |
| 18 | Re-running setup does not duplicate connections, identities, or repositories. | `test_ac18_setup_idempotent` | `worklog setup` run twice with identical answers; `connections`/`user_identities`/`repositories` row counts are unchanged between the two runs. |
| 19 | An expired Jira connection degrades gracefully without stopping local capture. | `test_ac19_expired_connection` | The loopback stub returns 401 on every request; `worklog jira refresh` marks the connection `expired` without losing the existing work item, and a subsequent Claude session (hooks) still captures its `git_commit_signal` event normally. |
| 20 | Backups and exports contain no secrets. | `test_ac20_no_secrets_in_exports` | A connection's credential is a real-looking token (via `env://`); `worklog backup`, `worklog export --format json/markdown/csv` are all produced and every artifact's raw bytes are grepped for the token literal — none found — and the JSON export's `connections.credential_reference` is `null`. |
| 21 | Backup and restore reproduce the same structured history. | `test_ac21_backup_restore_roundtrip` | A canonical dump of every structured table (`clients`, `projects`, `work_items`, `activities`, `remaining_items`, `remaining_item_transitions`) is taken, the live data is then deleted, `worklog backup` → `backup.restore(..., confirm=True)` is exercised, and the post-restore dump is asserted byte-for-byte equal to the pre-mutation one. |
| 22 | Daily reports default to summaries, with file evidence available on request. | `test_ac22_evidence_on_request` | `worklog today` omits the `evidence` key on an activity by default; `worklog today --evidence` includes it with the stored file reference. |
| 23 | Client/project reports include open work plus the previous seven workdays by default. | `test_ac23_status_window` | `timeutil.utc_now_iso` is pinned to a fixed "today"; activities are seeded at exactly 6 workdays back (the window's own edge — must appear) and 9 workdays back (outside it — must not). `worklog status <client>` is asserted on that actual content (which ticket's activity is present in `recent.clients` and which is not) and on the real computed `recent.start_workday`/`end_workday` boundaries, not merely on the echoed `window_workdays` argument or the literal `recent.kind`. |
| 24 | Jira is never modified by the plugin in version one. | `test_ac24_no_jira_writes` | A loopback `http.server` whose `do_POST`/`do_PUT`/`do_DELETE`/`do_PATCH` handlers record any call they receive is driven through a full session (hooks), a `jira refresh`, a `status` report, and a contradiction-resolution cycle; asserts the mutating-method list stayed empty throughout. Plus the static source grep from Task 38 (`tests/test_security.py::test_no_jira_write_verbs_anywhere`, reproduced inline here against `src/work_ledger/jira/*.py`): no `POST`/`PUT`/`DELETE`/`PATCH` literal or Jira write-tool name appears anywhere in the adapter package's source. |

## Stub caveats (criteria 13 and 15)

Neither of these two criteria can be verified end-to-end by an automated, offline,
network-free test suite — one requires a real, already-authenticated Jira MCP server
configured in Claude Code; the other requires a real macOS Keychain (or the
Linux/Windows equivalent) entry. Both test methods above verify the closest
programmatic proxy: the real discovery/adapter code path, driven against a
deterministic stand-in for the one external dependency that cannot be created inside
a test process. The manual procedure to close that last gap on a real environment:

- **Criterion 13 (Jira MCP reuse):** `worklog connections test <name>` against a
  connection whose `adapter` is `mcp` and whose MCP server is genuinely authenticated
  in Claude Code, and confirm it reports `healthy`.
- **Criterion 15 (Keychain storage):** `security find-generic-password -s work-ledger
  -a <connection-name>` on macOS after `worklog setup` has configured a direct
  (`jira-rest`) connection, and confirm the entry exists. **Precondition:** a keychain
  entry is only ever *created* when setup is given a raw `token` alongside a
  `keychain://` `credential_reference` (`setupwizard._store_token` only calls the
  credential store's `set()` when both are present). Answering with a
  `credential_reference: env://SOME_VAR` instead — pointing at a secret already sitting
  in an environment variable — never touches the keychain at all; that is by design
  (Spec §12.4: the reference and the storage mechanism are independent), not a setup
  failure.

## Full-suite regression

`docs/ACCEPTANCE.md` and `tests/acceptance/` are additive: they do not replace or
narrow any existing per-module test. Run the whole project suite (unit + acceptance)
with:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
