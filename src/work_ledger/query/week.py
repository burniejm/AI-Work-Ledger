"""Weekly digest and daily standup (README "Weekly digest & standup").

Both are reshapes of existing reports -- `range_report`/`day_report` for
the work, `status_report` for blockers -- no schema, no stored state.

Client-facing rule carries over from timesheets: the week digest hides
AI-usage and cost fields unless `internal=True`; the standup is always a
personal view and stays terse by design.
"""
from datetime import date, timedelta

from work_ledger import timeutil
from work_ledger.query import daily as query_daily
from work_ledger.query import status as query_status
from work_ledger.query import summaries as query_summaries
from work_ledger.query import timespent

_MAX_SUMMARIES_PER_ITEM = 5


def monday_of(workday: str) -> str:
    day = date.fromisoformat(workday)
    return (day - timedelta(days=day.weekday())).isoformat()


def _item_rows(client_node: dict, *, internal: bool) -> "list[dict]":
    items = []
    for project_node in client_node.get("projects") or []:
        for node in project_node.get("work_items") or []:
            summaries = query_summaries.dedupe(
                activity.get("summary") for activity in node.get("activities") or [])
            item = {
                "ticket": node.get("work_item"),
                "title": node.get("title"),
                "minutes": node.get("minutes") or 0,
                "summaries": summaries[:_MAX_SUMMARIES_PER_ITEM],
            }
            if internal and node.get("ai_usage"):
                item["ai_usage"] = node["ai_usage"]
            items.append(item)
    items.sort(key=lambda i: (-i["minutes"], str(i["ticket"])))
    return items


def week_report(conn, scope: "str | None", *, ending: str, tz: str,
                rollover_hour: int, internal: bool = False) -> dict:
    """Monday..`ending`, grouped client -> ticket with merged minutes."""
    resolved = query_status.resolve_scope(conn, scope)
    if resolved["kind"] not in ("all", "client"):
        raise query_status.ScopeNotFound(
            f"week digest takes a client (or nothing), but {scope!r}"
            f" is a {resolved['kind']}")

    start = monday_of(ending)
    scope_client_id = resolved["id"] if resolved["kind"] == "client" else None
    base = query_daily.range_report(conn, start, ending, tz=tz,
                                    rollover_hour=rollover_hour, client_id=scope_client_id)
    clients = []
    total_minutes = 0
    for client_node in base.get("clients") or []:
        entry = {
            "client": client_node.get("client"),
            "minutes": client_node.get("minutes") or 0,
            "items": _item_rows(client_node, internal=internal),
        }
        if internal and client_node.get("claude"):
            entry["claude"] = client_node["claude"]
        clients.append(entry)
        total_minutes += entry["minutes"]

    return {
        "kind": "week",
        "scope": resolved["label"],
        "start_workday": start,
        "end_workday": ending,
        "internal": internal,
        "clients": clients,
        "total_minutes": total_minutes,
    }


def render_week(report: dict, *, fmt: str = "text") -> str:
    if fmt == "md":
        return _render_week_lines(report, md=True)
    return _render_week_lines(report, md=False)


def _render_week_lines(report: dict, *, md: bool) -> str:
    title = (f"Week of {report['start_workday']} "
             f"({report['start_workday']} to {report['end_workday']})")
    lines = [f"# {title}" if md else title, ""]
    if not report["clients"]:
        lines.append("(nothing recorded this week)")
        return "\n".join(lines)
    for client in report["clients"]:
        heading = f"{client['client']} — {timespent.format_minutes(client['minutes'])}"
        lines.append(f"## {heading}" if md else heading)
        marker = "- " if md else "  "
        for item in client["items"]:
            summary = "; ".join(item["summaries"]) or item["title"] or ""
            extra = ""
            if item.get("ai_usage"):
                extra = f" [AI {item['ai_usage'].get('score')}/5]"
            lines.append(f"{marker}{item['ticket']} "
                         f"({timespent.format_minutes(item['minutes'])}){extra}: {summary}")
        claude = client.get("claude")
        if claude:
            from work_ledger.query import tokencost
            totals = claude.get("totals") or {}
            cost = claude.get("cost_usd")
            cost_text = f" (~${cost:.2f})" if cost is not None else ""
            lines.append(f"{marker}Claude: "
                         f"{tokencost.format_tokens(totals.get('input_tokens', 0))} in / "
                         f"{tokencost.format_tokens(totals.get('output_tokens', 0))} out"
                         f"{cost_text}")
        lines.append("")
    lines.append(f"Total: {timespent.format_minutes(report['total_minutes'])}")
    return "\n".join(lines)


def _day_lines(conn, workday: str, *, tz: str, rollover_hour: int,
               resolved: dict) -> "list[str]":
    """Activity lines for `workday`, scoped to `resolved` (any kind
    `resolve_scope` returns -- client/project/work_item/all)."""
    kind, scope_id = resolved["kind"], resolved["id"]
    report = query_daily.day_report(
        conn, workday, tz=tz, rollover_hour=rollover_hour,
        client_id=scope_id if kind == "client" else None)
    lines = []
    for client_node in report.get("clients") or []:
        for project_node in client_node.get("projects") or []:
            if kind == "project" and project_node.get("project_id") != scope_id:
                continue
            for node in project_node.get("work_items") or []:
                if kind == "work_item" and node.get("work_item_id") != scope_id:
                    continue
                summaries = query_summaries.dedupe(
                    activity.get("summary") for activity in node.get("activities") or [])
                if summaries:
                    lines.append(f"{node.get('work_item')}: "
                                 + "; ".join(summaries[:_MAX_SUMMARIES_PER_ITEM]))
    return lines


def standup_report(conn, scope: "str | None", *, tz: str, rollover_hour: int) -> dict:
    """Yesterday / Today / Blockers, terse by design."""
    resolved = query_status.resolve_scope(conn, scope)

    today = timeutil.workday_of(timeutil.utc_now_iso(), tz, rollover_hour)
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    status = query_status.status_report(conn, scope, tz=tz, rollover_hour=rollover_hour)
    blockers = []
    for item in (status.get("blockers") or []) + (status.get("waiting") or []):
        text = item.get("title") or ""
        if item.get("explanation"):
            text += f" — {item['explanation']}"
        blockers.append(text)

    return {
        "kind": "standup",
        "scope": resolved["label"],
        "yesterday": {"workday": yesterday,
                      "lines": _day_lines(conn, yesterday, tz=tz,
                                          rollover_hour=rollover_hour,
                                          resolved=resolved)},
        "today": {"workday": today,
                  "lines": _day_lines(conn, today, tz=tz,
                                      rollover_hour=rollover_hour,
                                      resolved=resolved)},
        "blockers": blockers,
    }


def render_standup(report: dict) -> str:
    lines = []
    for label, section in (("Yesterday", report["yesterday"]),
                           ("Today", report["today"])):
        lines.append(f"{label}: ({section['workday']})")
        if section["lines"]:
            lines.extend(f"  {line}" for line in section["lines"])
        else:
            lines.append("  (nothing recorded)")
    lines.append("Blockers:")
    if report["blockers"]:
        lines.extend(f"  {line}" for line in report["blockers"])
    else:
        lines.append("  (none)")
    return "\n".join(lines)
