"""Direct Jira Cloud REST adapter (Spec §11.2, §13; Global Constraints).

Read-only by construction: every request this module issues is a `GET`
(`urllib.request.Request(..., method="GET")` explicitly), and the module
contains no `POST`/`PUT`/`DELETE`/`PATCH` string anywhere -- there is no
write code path to accidentally exercise.

Credential handling: the bearer/basic-auth value is resolved fresh from
`credentials.resolve` on every request attempt (never cached on `self`,
never written to disk, never placed in the URL/query string -- only in the
`Authorization` header) and never logged. `AUTH_HEADER_BUILDERS` maps the
two supported `auth_method`s to the header value they produce.

Redirects are never followed. `urllib`'s default `HTTPRedirectHandler`
forwards the `Authorization` header verbatim to the redirect target -- even
a different host -- which would leak the resolved credential off-target. A
read-only issue/health client has no legitimate reason to be redirected, so
`_OPENER` installs a handler that always refuses (`redirect_request`
returns `None`); the unhandled 3xx then surfaces as a normal `HTTPError`
that `_get` maps like any other non-2xx response, and no second request is
ever issued.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from work_ledger import credentials
from work_ledger.jira.adapter import (
    Capability,
    HealthResult,
    IdentityResult,
    IssueResult,
    JiraAdapter,
)


class IssueNotFound(Exception):
    """`get_issue` was asked for a key Jira returned 404 for."""


class InvalidIssueKey(Exception):
    """`get_issue` was asked for a string that isn't a well-formed Jira
    issue key (`PROJECT-123`). Rejected before any request is built --
    never interpolated unescaped into a URL path."""


class NotFound(Exception):
    """A GET returned HTTP 404. Raised by `_get` for any endpoint; only
    `get_issue` translates this into the more specific `IssueNotFound` --
    a 404 from `/myself`, `/search/jql`, or `/project/search` does not mean
    "no such issue", so other callers see this generic, clearly-named error
    instead."""


class JiraAuthError(Exception):
    """The request failed with 401/403 -- the credential is invalid or expired."""


class JiraUnavailable(Exception):
    """The request could not complete (connection error, timeout, a
    redirect response, a corrupt-JSON body, or a 5xx that survived the
    single retry)."""


def _cloud_api_token_header(username: "str | None", token: str) -> str:
    raw = f"{username or ''}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _pat_header(username: "str | None", token: str) -> str:
    return "Bearer " + token


AUTH_HEADER_BUILDERS: "dict[str, Callable[[str, str], str]]" = {
    "cloud-api-token": _cloud_api_token_header,
    "pat": _pat_header,
}

# Jira Cloud issue key: a project key (letter, then letters/digits) then a
# dash then a number, e.g. "AB-76". Anything else is rejected by `get_issue`
# before it ever reaches URL construction.
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-[0-9]+$")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses every 3xx redirect -- see the module docstring for why."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Built once: stateless, and subclassing HTTPRedirectHandler means
# build_opener drops the default (redirect-following) handler in favor of
# this one.
_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class JiraRestAdapter(JiraAdapter):
    """Talks to Jira Cloud's REST API v3 directly over HTTPS (or HTTP, in
    tests, against a local `http.server` fixture). No third-party HTTP
    client -- `urllib.request` only, per the Global Constraints' stdlib-only
    runtime rule."""

    def __init__(
        self,
        base_url: str,
        username: "str | None",
        credential_reference: str,
        *,
        auth_method: str = "cloud-api-token",
        timeout: int = 10,
    ):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._credential_reference = credential_reference
        self._auth_method = auth_method
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._base_url

    def capabilities(self) -> "dict[str, bool]":
        supported = {
            Capability.IDENTIFY_CURRENT_USER,
            Capability.LIST_PROJECTS,
            Capability.GET_ISSUE,
            Capability.SEARCH_ISSUES,
            Capability.GET_ISSUE_HISTORY,
        }
        return {c.value: (c in supported) for c in Capability}

    def _auth_header(self) -> "str | None":
        token = credentials.resolve(self._credential_reference)
        if token is None:
            return None
        builder = AUTH_HEADER_BUILDERS[self._auth_method]
        return builder(self._username, token)

    def _get(self, path: str, query: "dict[str, str] | None" = None) -> dict:
        """Issue one GET request to `path` (with optional query params),
        retrying once after a 1s pause on a 5xx or a connection
        error/timeout. Never a 4xx retry, never a redirect. Returns the
        parsed JSON body.

        A malformed `base_url` (missing/unknown scheme, etc.) raises
        `ValueError` out of `urlopen`/`Request` before any connection is
        attempted; deliberately left uncaught here (not retried, not folded
        into `JiraUnavailable`) so `health()` can tell "can't even form a
        request" (-> missing) apart from "tried to connect and failed"
        (-> degraded)."""
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        attempts_left = 2
        while True:
            attempts_left -= 1
            headers = {"Accept": "application/json"}
            auth_header = self._auth_header()
            if auth_header is not None:
                headers["Authorization"] = auth_header
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with _OPENER.open(request, timeout=self._timeout) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise NotFound(f"not found: {path}") from None
                if exc.code in (401, 403):
                    raise JiraAuthError(f"authentication failed: HTTP {exc.code}") from None
                if exc.code >= 500 and attempts_left > 0:
                    time.sleep(1)
                    continue
                # Includes 3xx: `_NoRedirectHandler` refuses to follow, so a
                # redirect response surfaces here as an ordinary HTTPError
                # rather than a second request.
                raise JiraUnavailable(f"HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempts_left > 0:
                    time.sleep(1)
                    continue
                raise JiraUnavailable(str(type(exc).__name__)) from None
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise JiraUnavailable(f"invalid JSON response: {exc}") from None

    def identify_current_user(self) -> IdentityResult:
        body = self._get("/rest/api/3/myself")
        return IdentityResult(
            account_id=body.get("accountId"),
            display_name=body.get("displayName"),
            email=body.get("emailAddress"),
        )

    def get_issue(self, key: str) -> IssueResult:
        if not isinstance(key, str) or not _ISSUE_KEY_RE.match(key):
            raise InvalidIssueKey(f"not a well-formed Jira issue key: {key!r}")
        try:
            body = self._get(
                f"/rest/api/3/issue/{urllib.parse.quote(key, safe='')}",
                {"fields": "summary,status,resolution,assignee,project,updated"},
            )
        except NotFound:
            raise IssueNotFound(f"no such issue: {key}") from None
        return IssueResult.from_cloud_payload(body)

    def search_issues(self, jql: str, limit: int = 50) -> "list[IssueResult]":
        body = self._get(
            "/rest/api/3/search/jql",
            {"jql": jql, "maxResults": str(limit)},
        )
        return [IssueResult.from_cloud_payload(issue) for issue in body.get("issues", [])]

    def list_projects(self) -> list:
        body = self._get("/rest/api/3/project/search")
        return body.get("values", [])

    def health(self) -> HealthResult:
        checked_at = _now_iso()
        try:
            self.identify_current_user()
            return HealthResult(state="healthy", detail=None, checked_at=checked_at)
        except JiraAuthError as exc:
            return HealthResult(state="expired", detail=str(exc), checked_at=checked_at)
        except NotFound as exc:
            return HealthResult(state="degraded", detail=str(exc), checked_at=checked_at)
        except JiraUnavailable as exc:
            return HealthResult(state="degraded", detail=str(exc), checked_at=checked_at)
        except ValueError as exc:
            # A malformed base_url (no/unknown scheme) fails to even form a
            # request -- ValueError from urlopen/Request, never retried,
            # never folded into JiraUnavailable (see `_get`). This is NOT
            # the same ValueError subclass as a corrupt-JSON body
            # (json.JSONDecodeError), which `_get` already translates into
            # JiraUnavailable above -- so it never reaches this branch.
            return HealthResult(state="missing", detail=str(exc), checked_at=checked_at)
