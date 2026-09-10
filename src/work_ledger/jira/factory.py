"""Adapter construction (Ruling 13).

This is the one place a `connections` row becomes a live `JiraAdapter`, and
it deliberately does **not** live in `connections.py`. `jira.mcp` (and
Task 35's refresh path) import `connections` for
`list_connections`/`record_capabilities`/`set_health`; if `connections` also
imported `jira.rest`/`jira.mcp` to build adapters, that would be a genuine
import cycle. Keeping construction here makes the direction one-way:

    connections  ->  jira.adapter            (the capability vocabulary only)
    jira.factory ->  jira.adapter, jira.rest, jira.mcp

so this module imports, from `work_ledger`, nothing but `config` and the
three `jira` modules. In particular it must never import `connections` --
`tests/test_jira_mcp.py` walks this module's AST to check exactly that, and
Task 38's `test_layering.py` enforces the whole direction. Callers that live
in `connections` (`worklog connections test/repair`) reach `build_adapter`
through a *function-local* import, which leaves the module-scope graph
acyclic.

Read-only, like everything under `jira/`: this module chooses between two
adapters, neither of which has a write method.
"""
from work_ledger import config
from work_ledger.jira.adapter import JiraAdapter
from work_ledger.jira.mcp import JiraMcpAdapter
from work_ledger.jira.rest import AUTH_HEADER_BUILDERS, JiraRestAdapter

__all__ = ["UnknownAdapter", "ADAPTERS", "build_adapter"]

# The `connections.adapter` CHECK constraint allows exactly these two.
ADAPTERS = ("mcp", "jira-rest")

# `connections.auth_method` also allows 'mcp-oauth', which is not a REST
# auth scheme -- a row carrying it falls back to Jira Cloud's Basic
# email:api-token rather than raising a KeyError deep inside a request.
_DEFAULT_AUTH_METHOD = "cloud-api-token"


class UnknownAdapter(Exception):
    """`connection_row['adapter']` is not one of `ADAPTERS`. Raised rather
    than defaulted: silently treating an unrecognized adapter as REST would
    send credentials somewhere nobody asked for."""


def _field(row, key, default=None):
    """Read `key` from a `sqlite3.Row` or a plain dict (`sqlite3.Row` raises
    `IndexError` for a column it does not have)."""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def build_adapter(conn, connection_row) -> JiraAdapter:
    """Build the adapter this connection row describes.

    `conn` is threaded into `JiraMcpAdapter` so its capability and health
    reads use the caller's ledger connection rather than opening their own.
    The REST adapter takes no connection: it holds only a base URL, a
    username, and a *credential reference* -- never a secret, which it
    resolves per request.
    """
    adapter = _field(connection_row, "adapter")
    if adapter == "mcp":
        return JiraMcpAdapter(connection_row, conn=conn)
    if adapter == "jira-rest":
        auth_method = _field(connection_row, "auth_method", _DEFAULT_AUTH_METHOD)
        if auth_method not in AUTH_HEADER_BUILDERS:
            auth_method = _DEFAULT_AUTH_METHOD
        return JiraRestAdapter(
            _field(connection_row, "base_url", ""),
            _field(connection_row, "username"),
            _field(connection_row, "credential_reference", ""),
            auth_method=auth_method,
            timeout=config.get("jira.request_timeout_seconds", 10),
        )
    raise UnknownAdapter(
        f"unknown adapter {adapter!r} for connection "
        f"{_field(connection_row, 'name', '<unnamed>')!r} (expected one of {ADAPTERS})"
    )
