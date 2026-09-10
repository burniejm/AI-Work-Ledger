"""Jira adapter interface and result types (Spec §11.2, §13; Global Constraints).

Jira is read-only in v1: `JiraAdapter` defines no write method, and nothing
in this module builds a POST/PUT/DELETE request. `jira.rest` (Task 34's MCP
adapter, `jira.mcp`, and Task 34's `jira.factory`) implement this interface;
this module itself is a leaf -- it imports nothing else from
`work_ledger.jira` or from `work_ledger.connections` -- so `connections.py`
can import it for the capability vocabulary alone (Ruling 13) without
creating an import cycle.

Ruling 27 -- capabilities are plain strings at every boundary. `Capability`
is the vocabulary (`Capability.GET_ISSUE.value == "get_issue"`, and
`CAPABILITY_VALUES` is the allowed key set), but every dict that crosses a
module or storage boundary (`JiraAdapter.capabilities()`,
`connections.record_capabilities`, `connection_capabilities.capability`, and
Task 34's `jira.mcp.CAPABILITY_TOOL_HINTS`/`map_capabilities`) is keyed by
the plain `str` value, never by the enum member itself.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Capability(Enum):
    """The Jira capabilities work-ledger knows how to probe for and use.

    Exact string values from Spec §11.2. Deliberately a plain `Enum` (not
    mixed in with `str`) rather than `IntEnum`/`StrEnum`: a plain `Enum`
    member hashes and compares by identity, not by its `.value`, so a dict
    accidentally keyed by the member itself (`{Capability.GET_ISSUE: True}`)
    is unreachable by the string "get_issue" -- the mistake Ruling 27 exists
    to prevent surfaces immediately as a `KeyError` instead of silently
    working by coincidence. Callers must always cross a boundary with
    `.value`.
    """

    IDENTIFY_CURRENT_USER = "identify_current_user"
    LIST_SITES = "list_sites"
    LIST_PROJECTS = "list_projects"
    GET_ISSUE = "get_issue"
    SEARCH_ISSUES = "search_issues"
    GET_ISSUE_HISTORY = "get_issue_history"
    GET_USER_ACTIVITY = "get_user_activity"


CAPABILITY_VALUES: "frozenset[str]" = frozenset(c.value for c in Capability)

# Spec §13's six connection health states (also the `connections.health_state`
# CHECK constraint in 002_environment.sql).
HEALTH_STATES: "frozenset[str]" = frozenset(
    {"healthy", "degraded", "expired", "missing", "unsupported", "disabled"}
)


@dataclass
class IssueResult:
    """A normalized Jira issue. Every field is `None` when the source
    payload omitted it -- `from_cloud_payload` never raises on a sparse or
    partial issue payload."""

    key: "str | None"
    id: "str | None"
    summary: "str | None"
    status: "str | None"
    status_category: "str | None"
    resolution: "str | None"
    assignee_account_id: "str | None"
    assignee_display_name: "str | None"
    project_key: "str | None"
    updated: "str | None"
    url: "str | None"

    @classmethod
    def from_cloud_payload(cls, d: dict) -> "IssueResult":
        """Normalize a Jira Cloud v3 issue payload (`GET /rest/api/3/issue/{key}`
        shape, and the shape of each hit in a `/search` result) defensively:
        missing fields become `None` rather than raising `KeyError`."""
        fields = d.get("fields") or {}
        status_obj = fields.get("status") or {}
        status_category_obj = status_obj.get("statusCategory") or {}
        resolution = fields.get("resolution")
        if isinstance(resolution, dict):
            resolution = resolution.get("name")
        assignee = fields.get("assignee") or {}
        project = fields.get("project") or {}
        return cls(
            key=d.get("key"),
            id=d.get("id"),
            summary=fields.get("summary"),
            status=status_obj.get("name"),
            status_category=status_category_obj.get("key"),
            resolution=resolution,
            assignee_account_id=assignee.get("accountId"),
            assignee_display_name=assignee.get("displayName"),
            project_key=project.get("key"),
            updated=fields.get("updated"),
            url=d.get("self"),
        )


@dataclass
class IdentityResult:
    """The identity of the currently-authenticated Jira user."""

    account_id: "str | None"
    display_name: "str | None"
    email: "str | None"


@dataclass
class HealthResult:
    """The outcome of a connection health probe. `state` is one of
    `HEALTH_STATES` (Spec §13)."""

    state: str
    detail: "str | None"
    checked_at: str


class JiraAdapter(ABC):
    """Common interface every Jira adapter implements (the MCP-backed
    adapter and the direct Cloud REST adapter, both in Task 34).

    Read-only by construction: this ABC defines no write method (no
    `create_issue`/`update_issue`/`transition_issue`/`add_comment`/
    `add_worklog`/`delete_issue`, no `post`/`put`/`delete`). Adding one is a
    spec violation (Global Constraints: "Jira is read-only in v1").
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """The `connections.name` this adapter instance was built for."""

    @abstractmethod
    def capabilities(self) -> dict[str, bool]:
        """Which `CAPABILITY_VALUES` this adapter instance supports, keyed
        by the plain string value (Ruling 27) -- built with
        `Capability.GET_ISSUE.value`, never the enum member."""

    @abstractmethod
    def identify_current_user(self) -> IdentityResult:
        """Identify the account this adapter is authenticated as."""

    @abstractmethod
    def get_issue(self, key: str) -> IssueResult:
        """Fetch a single issue by its key (e.g. `AB-76`)."""

    @abstractmethod
    def search_issues(self, jql: str, limit: int = 50) -> "list[IssueResult]":
        """Run a JQL search, returning up to `limit` normalized issues."""

    @abstractmethod
    def health(self) -> HealthResult:
        """Probe this connection's current health."""
