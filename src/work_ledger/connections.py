"""Jira connection persistence and health probing (Ruling 13).

This module's own module-scope import graph never names a live Jira adapter
module. Its top-level imports are limited to `db.connection`, `ids`,
`timeutil`, `entities`, `credentials`, and -- for the capability
*vocabulary* only -- `jira.adapter` (a leaf module that imports nothing from
this module). It must never import `jira.rest`, `jira.mcp`, or
`jira.factory` at module scope: those modules import *from* here
(`record_capabilities`/`set_health`/`list_connections`), and a module-scope
import back would recreate the cycle Ruling 13 exists to break.

`test_connection`/`repair` (Task 35) are the one exception, and a narrow
one: they DO construct and call a live adapter -- that is the whole point of
"test this connection" -- but they reach `jira.factory.build_adapter` via a
lazily-imported default (`_default_builder`, imported inside its own
function body, never at module load), so the module-scope graph stays
acyclic. `test_layering.py` (Task 38) enforces the module-scope direction
with `ast`, so a reintroduced module-scope cycle fails the suite rather than
surviving review.
"""
import json
import urllib.parse

from work_ledger import credentials, entities, ids, redact, timeutil
from work_ledger.db import connection as dbc
from work_ledger.jira import adapter as jira_adapter


_UNSAFE_REFERENCE_MSG = (
    "credential_reference must be a keychain:// or env:// reference, not a "
    "raw secret"
)
_UNSAFE_BASE_URL_MSG = (
    "base_url must not contain embedded credentials (scheme://user:pass@host "
    "is not accepted, nor a value that looks like a secret)"
)


def _validate_credential_reference(value: "str | None") -> None:
    """Raise `ValueError` (Important #1) unless `value` is either `None` or
    a genuine `keychain://`/`env://` reference. This is the module boundary
    every caller of `upsert_connection`/`repair` goes through -- CLI's
    `connections repair --credential-reference` included -- so a pasted raw
    secret is refused before it ever reaches SQLite (from which
    `connections list --format json` would otherwise echo it back).

    Mirrors `setupwizard._is_safe_reference`'s two-part check (ask
    `credentials.parse_reference` what a reference looks like, then let
    `redact.looks_like_secret` have the final say) but does not call it
    directly -- that function lives in a module this one must not import at
    module scope (see the module docstring). The error message never
    echoes `value`: it may itself be the secret.
    """
    if value is None:
        return
    try:
        credentials.parse_reference(value)
    except credentials.InvalidReference:
        raise ValueError(_UNSAFE_REFERENCE_MSG) from None
    if redact.looks_like_secret(value):
        raise ValueError(_UNSAFE_REFERENCE_MSG)


def _validate_base_url(value: "str | None") -> None:
    """Raise `ValueError` (Important #1) if `value` carries URL userinfo
    (`scheme://user:pass@host`) or otherwise looks like a secret. A bare
    hostname URL is otherwise unconstrained."""
    if value is None:
        return
    if redact.looks_like_secret(value):
        raise ValueError(_UNSAFE_BASE_URL_MSG)
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password:
        raise ValueError(_UNSAFE_BASE_URL_MSG)


def upsert_connection(conn, *, name: str, adapter: str, base_url: "str | None" = None,
                       cloud_id: "str | None" = None, username: "str | None" = None,
                       auth_method: "str | None" = None,
                       credential_reference: "str | None" = None,
                       mcp_server_name: "str | None" = None, client_hint: "str | None" = None,
                       ticket_patterns: "list[str] | None" = None,
                       project_filter: "list[str] | None" = None) -> str:
    """Return a live connection's id, matching on `name` (Spec §20.18:
    idempotent on `name` -- a second call with the same name updates the
    existing row rather than creating a sibling).

    Every optional field is "only-when-supplied" on an existing row (same
    pattern as `entities.upsert_repository`): a re-call that omits one
    leaves that field's previously-set value in place. `adapter` has no such
    ambiguity -- it is required on every call and always (re)applied.
    `ticket_patterns`/`project_filter`, when supplied, *replace* the stored
    list wholesale (they are not merged) -- callers pass the full desired
    list each time.
    """
    _validate_credential_reference(credential_reference)
    _validate_base_url(base_url)
    device_id = entities.current_device(conn)
    now = timeutil.utc_now_iso()
    with dbc.transaction(conn):
        row = conn.execute(
            "SELECT id FROM connections WHERE name=? AND deleted_at IS NULL", (name,)
        ).fetchone()
        if row is not None:
            sets = ["adapter=?"]
            params = [adapter]
            if base_url is not None:
                sets.append("base_url=?"); params.append(base_url)
            if cloud_id is not None:
                sets.append("cloud_id=?"); params.append(cloud_id)
            if username is not None:
                sets.append("username=?"); params.append(username)
            if auth_method is not None:
                sets.append("auth_method=?"); params.append(auth_method)
            if credential_reference is not None:
                sets.append("credential_reference=?"); params.append(credential_reference)
            if mcp_server_name is not None:
                sets.append("mcp_server_name=?"); params.append(mcp_server_name)
            if client_hint is not None:
                sets.append("client_hint=?"); params.append(client_hint)
            if ticket_patterns is not None:
                sets.append("ticket_patterns_json=?"); params.append(json.dumps(ticket_patterns))
            if project_filter is not None:
                sets.append("project_filter_json=?"); params.append(json.dumps(project_filter))
            sets.append("updated_at=?")
            params.append(now)
            params.append(row["id"])
            conn.execute(f"UPDATE connections SET {', '.join(sets)} WHERE id=?", params)
            return row["id"]

        connection_id = ids.new_id()
        conn.execute(
            "INSERT INTO connections(id, name, adapter, base_url, cloud_id, username, "
            "auth_method, credential_reference, mcp_server_name, client_hint, "
            "ticket_patterns_json, project_filter_json, origin_device_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (connection_id, name, adapter, base_url, cloud_id, username, auth_method,
             credential_reference, mcp_server_name, client_hint,
             json.dumps(ticket_patterns if ticket_patterns is not None else []),
             json.dumps(project_filter if project_filter is not None else []),
             device_id, now, now),
        )
        return connection_id


def list_connections(conn, *, include_disabled: bool = False) -> "list":
    """Return live connections, ordered by name. `enabled=0` connections are
    excluded unless `include_disabled=True`."""
    if include_disabled:
        return list(conn.execute(
            "SELECT * FROM connections WHERE deleted_at IS NULL ORDER BY name"
        ).fetchall())
    return list(conn.execute(
        "SELECT * FROM connections WHERE deleted_at IS NULL AND enabled=1 ORDER BY name"
    ).fetchall())


def record_capabilities(conn, connection_id: str, caps: "dict[str, bool]", *,
                         tool_names: "dict[str, str] | None" = None) -> None:
    """Upsert on `(connection_id, capability)` (the `uq_conn_capability`
    unique index) -- a re-probe updates `supported`/`tool_name`/`checked_at`
    on the existing row rather than duplicating it.

    `caps` and `tool_names` are keyed by plain strings from
    `jira.adapter.CAPABILITY_VALUES` (Ruling 27), never by the `Capability`
    enum member. Any key outside that vocabulary raises `ValueError`.
    """
    invalid = set(caps) - jira_adapter.CAPABILITY_VALUES
    if invalid:
        raise ValueError(f"unknown capability keys: {sorted(invalid)}")
    tool_names = tool_names or {}
    now = timeutil.utc_now_iso()
    with dbc.transaction(conn):
        for capability, supported in caps.items():
            tool_name = tool_names.get(capability)
            row = conn.execute(
                "SELECT id FROM connection_capabilities WHERE connection_id=? AND capability=?",
                (connection_id, capability),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE connection_capabilities SET supported=?, tool_name=?, "
                    "checked_at=?, updated_at=? WHERE id=?",
                    (int(bool(supported)), tool_name, now, now, row["id"]),
                )
            else:
                capability_id = ids.new_id()
                conn.execute(
                    "INSERT INTO connection_capabilities(id, connection_id, capability, "
                    "supported, tool_name, checked_at, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (capability_id, connection_id, capability, int(bool(supported)),
                     tool_name, now, now, now),
                )


def set_health(conn, connection_id: str, state: str, detail: "str | None",
                *, hook_mode: bool = False) -> None:
    """Record the outcome of a connection health probe (Spec §13). `state`
    is validated by the `connections.health_state` CHECK constraint --
    an unrecognized value raises `sqlite3.IntegrityError`.

    `hook_mode=True` threads through to `db.connection.transaction` for the
    hook's bounded busy-retry schedule -- `jira.refresh`'s hook-time callers
    (`refresh_rest`/`refresh_all_rest`) pass it through (Ruling 8)."""
    now = timeutil.utc_now_iso()
    with dbc.transaction(conn, hook_mode=hook_mode):
        conn.execute(
            "UPDATE connections SET health_state=?, health_detail=?, health_checked_at=?, "
            "updated_at=? WHERE id=?",
            (state, detail, now, now, connection_id),
        )


def remove(conn, name: str, *, delete_credential: bool = False, confirmed: bool = False) -> dict:
    """Soft-delete the connection named `name` and clear its
    `credential_reference` (Spec §14.1). The actual secret in the credential
    store is deleted only when both `delete_credential` and `confirmed` are
    true -- `delete_credential=True` alone (or with `confirmed=False`) is
    refused with `ValueError` rather than silently skipped, so a caller
    can't accidentally believe a secret was deleted when it wasn't.

    Returns `{"connection_id": ..., "credential_deleted": bool}`.
    """
    if delete_credential and not confirmed:
        raise ValueError(
            "delete_credential requires confirmed=True (Spec §14.1: credential "
            "deletion is never implicit)"
        )

    row = conn.execute(
        "SELECT id, credential_reference FROM connections WHERE name=? AND deleted_at IS NULL",
        (name,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no live connection named {name!r}")

    connection_id = row["id"]
    reference = row["credential_reference"]
    now = timeutil.utc_now_iso()
    with dbc.transaction(conn):
        conn.execute(
            "UPDATE connections SET credential_reference=NULL, deleted_at=?, updated_at=? "
            "WHERE id=?",
            (now, now, connection_id),
        )

    credential_deleted = False
    if delete_credential and confirmed and reference and credentials.resolve(reference) is not None:
        try:
            store = credentials.get_store(reference)
            store.delete(reference)
            credential_deleted = True
        except (credentials.Unsupported, credentials.InvalidReference):
            credential_deleted = False

    return {"connection_id": connection_id, "credential_deleted": credential_deleted}


def _default_builder():
    """Lazily import `jira.factory.build_adapter` (Ruling 13). This function
    body is the only place in this module that names a `jira.*` module other
    than the leaf `jira.adapter` -- the import happens at call time, not at
    module load, so `connections.py`'s own module-scope import graph stays
    free of a `jira.rest`/`jira.mcp`/`jira.factory` edge (what Task 38's
    `test_layering.py` walks with `ast`)."""
    from work_ledger.jira.factory import build_adapter
    return build_adapter


def _get_live_connection(conn, name: str):
    row = next(
        (c for c in list_connections(conn, include_disabled=True) if c["name"] == name), None
    )
    if row is None:
        raise ValueError(f"no connection named {name!r}")
    return row


def test_connection(conn, name: str, *, build=None) -> dict:
    """Run the Spec §13 validation list applicable to this connection's
    adapter and report the outcome, without changing anything but the
    connection's own `health_*` columns (Spec §19).

    For `adapter='jira-rest'`: `authentication` (the credential reference
    resolves to a secret), `current_user` (`identify_current_user()`
    succeeds), and `issue_retrieval` (`get_issue()` succeeds against one of
    this connection's own already-known issue keys, or `skipped` when it has
    none yet to sample).

    For `adapter='mcp'`: nothing here ever calls a live MCP tool (DESIGN gap
    G2 -- a script has no channel to one); `authentication` is `skipped`
    (MCP credentials are the connected server's to manage, never this
    connection's `credential_reference`), and `current_user`/
    `issue_retrieval` read the capabilities setup already recorded via
    `record_capabilities`.

    Returns `{"name": ..., "state": ..., "checks": [{"name", "state",
    "detail"}, ...]}`, where `state` is one of `jira.adapter.HEALTH_STATES`.
    """
    build = build or _default_builder()
    row = _get_live_connection(conn, name)
    adapter = build(conn, row)
    checks: "list[dict]" = []

    if row["adapter"] == "mcp":
        caps = adapter.capabilities()
        checks.append({
            "name": "authentication", "state": "skipped",
            "detail": "MCP credentials are managed by the connected MCP server, not work-ledger",
        })
        for capability in ("identify_current_user", "get_issue"):
            check_name = "current_user" if capability == "identify_current_user" else "issue_retrieval"
            supported = bool(caps.get(capability))
            checks.append({
                "name": check_name,
                "state": "ok" if supported else "fail",
                "detail": None if supported else f"{capability} is not a supported capability",
            })
    else:
        auth_detail = None
        try:
            reference = row["credential_reference"]
            token = credentials.resolve(reference) if reference else None
        except Exception as exc:  # narrowly scoped to this one check
            token = None
            auth_detail = str(exc)
        auth_state = "ok" if token is not None else "fail"
        if auth_state == "fail" and auth_detail is None:
            auth_detail = "credential could not be resolved"
        checks.append({"name": "authentication", "state": auth_state, "detail": auth_detail})

        # Code review MINOR: `JiraRestAdapter.health()` IS an
        # `identify_current_user()` probe (it calls it and classifies the
        # outcome) -- calling `identify_current_user()` again here just for
        # this check would be a second, redundant network round trip for the
        # same fact. `health` is computed once, up front, and reused both for
        # this check and for the overall `state`/`set_health` call below.
        health = adapter.health()
        checks.append({
            "name": "current_user",
            "state": "ok" if health.state == "healthy" else "fail",
            "detail": health.detail,
        })

        sample = conn.execute(
            "SELECT external_key FROM work_items WHERE connection_id=? AND kind='jira' "
            "AND deleted_at IS NULL AND merged_into_id IS NULL AND external_key IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if sample is None:
            ir_state, ir_detail = "skipped", "no Jira-backed work item to sample"
        else:
            ir_state, ir_detail = "ok", None
            try:
                adapter.get_issue(sample["external_key"])
            except Exception as exc:
                ir_state, ir_detail = "fail", str(exc)
        checks.append({"name": "issue_retrieval", "state": ir_state, "detail": ir_detail})

    if row["adapter"] == "mcp":
        # No live call was made above (capability flags only), so this is
        # the only probe -- cheap, since `JiraMcpAdapter.health()` just
        # checks the recorded capability probe's age, never a live call.
        health = adapter.health()
    set_health(conn, row["id"], health.state, health.detail)
    return {"name": name, "state": health.state, "checks": checks}


def repair(conn, name: str, *, credential_reference: "str | None" = None, build=None) -> dict:
    """Re-resolve the credential (optionally replacing
    `credential_reference` first) and re-run `test_connection`, updating
    health without deleting any history (Spec §19) -- no `work_items`,
    `activities`, `remaining_items`, or `contradictions` row is ever touched
    here.
    """
    row = _get_live_connection(conn, name)
    if credential_reference is not None:
        _validate_credential_reference(credential_reference)
        now = timeutil.utc_now_iso()
        with dbc.transaction(conn):
            conn.execute(
                "UPDATE connections SET credential_reference=?, updated_at=? WHERE id=?",
                (credential_reference, now, row["id"]),
            )
    return test_connection(conn, name, build=build)
