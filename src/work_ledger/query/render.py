"""Plain-text and JSON rendering for query reports (`query.daily`,
`query.status`, `query.scoped`).

This module only formats what's already in the report dict -- it never
reaches back into the database, so a caller that built a day/range report
with `include_evidence=False` (the default; Spec 20.22) gets text with no
file paths, commit shas, or excerpts in it, full stop. `query.scoped`'s
`evidence_report` is the one report kind whose whole purpose is to surface
evidence refs/excerpts -- those are rendered here because the caller asked
for them explicitly, and they're already redacted/length-capped at write
time (Global Constraints: evidence storage), never full file contents.
"""
import json

__all__ = ["render_report"]


def render_report(report: dict, *, fmt: str = "text") -> str:
    if fmt == "json":
        return json.dumps(report, indent=2)
    return _render_text(report)


def _header_line(client: dict, project: dict, work_item: dict) -> str:
    key = work_item.get("work_item")
    title = work_item.get("title")
    if key and title:
        target = f"[{key}] {title}"
    elif key:
        target = f"[{key}]"
    else:
        target = title or "(untitled)"
    return f"{client['client']} / {project['project']} / {target}"


def _render_text(report: dict) -> str:
    kind = report.get("kind")
    renderer = {
        "day": _render_daily,
        "range": _render_daily,
        "status": _render_status,
        "remaining": _render_remaining,
        "blocked": _render_blocked,
        "stale": _render_stale,
        "evidence": _render_evidence,
        "inbox": _render_inbox,
    }.get(kind, _render_daily)
    return renderer(report)


def _render_daily(report: dict) -> str:
    lines = []
    kind = report.get("kind")
    if kind == "day":
        lines.append(f"Workday {report.get('workday')}")
    elif kind == "range":
        lines.append(f"Range {report.get('start_workday')} .. {report.get('end_workday')}")
    lines.append("")

    clients = report.get("clients") or []
    if not clients:
        lines.append("(no activity)")

    for client in clients:
        for project in client.get("projects", []):
            for work_item in project.get("work_items", []):
                lines.append(_header_line(client, project, work_item))

                jira = work_item.get("jira")
                if jira:
                    lines.append(f"  jira: {jira.get('status')} ({jira.get('assignee') or 'unassigned'})")
                ai_usage = work_item.get("ai_usage")
                if ai_usage:
                    lines.append(f"  AI {ai_usage.get('score')}/5 ({ai_usage.get('confidence')})")

                for activity in work_item.get("activities", []):
                    markers = []
                    if activity.get("inferred"):
                        markers.append("(inferred)")
                    if activity.get("conflict"):
                        markers.append("⚠ conflict")
                    marker_text = f" {' '.join(markers)}" if markers else ""
                    lines.append(f"  - {activity.get('summary')}{marker_text}")
                    for ev in activity.get("evidence") or []:
                        ref = ev.get("ref")
                        if ref:
                            lines.append(f"      evidence: {ev.get('type')} {ref}")

                for item in work_item.get("remaining_discovered", []):
                    lines.append(f"  remaining: {item.get('title')} ({item.get('state')})")
                for item in work_item.get("blockers", []):
                    lines.append(f"  blocker: {item.get('title')} ({item.get('state')})")
                for decision in work_item.get("decisions", []):
                    lines.append(f"  decision: {decision.get('statement')}")
                for contradiction in work_item.get("contradictions", []):
                    lines.append(
                        f"  ⚠ contradiction ({contradiction.get('rule')}): "
                        f"ledger says {contradiction.get('ledger_claim')!r}, "
                        f"external says {contradiction.get('external_claim')!r}"
                    )
                lines.append("")

    excluded = report.get("excluded_low_confidence_count", 0)
    if excluded:
        lines.append(f"({excluded} low-confidence item(s) excluded; use --include-inbox to see them)")

    for note in report.get("notes") or []:
        lines.append(f"note: {note}")

    return "\n".join(lines)


def _scope_label(report: dict) -> str:
    scope = report.get("scope") or {}
    return scope.get("label", "all")


def _render_remaining_item(item: dict, *, with_explanation: bool = False) -> str:
    label = f"[{item.get('work_item')}] " if item.get("work_item") else ""
    line = f"  - {label}{item.get('title')} ({item.get('state')})"
    if with_explanation and item.get("explanation"):
        line += f" -- {item.get('explanation')}"
    return line


def _render_status(report: dict) -> str:
    lines = [f"Status: {_scope_label(report)}", ""]

    ai_usage = report.get("ai_usage")
    if ai_usage:
        lines.append(f"AI usage: {ai_usage.get('score')}/5 ({ai_usage.get('confidence')})"
                     f" — {ai_usage.get('reason')}")
        lines.append("")

    lines.append(f"Open work ({len(report.get('open_work') or [])}):")
    for item in report.get("open_work") or []:
        lines.append(_render_remaining_item(item))
    lines.append("")

    if report.get("blockers"):
        lines.append("Blocked:")
        for item in report["blockers"]:
            lines.append(_render_remaining_item(item, with_explanation=True))
        lines.append("")

    if report.get("waiting"):
        lines.append("Waiting:")
        for item in report["waiting"]:
            lines.append(_render_remaining_item(item, with_explanation=True))
        lines.append("")

    if report.get("jira"):
        lines.append("Jira:")
        for entry in report["jira"]:
            marker = " (stale)" if entry.get("stale") else ""
            lines.append(f"  [{entry.get('work_item')}] {entry.get('status')}{marker}")
        lines.append("")

    if report.get("stale_items"):
        lines.append("Stale:")
        for item in report["stale_items"]:
            lines.append(f"  [{item.get('work_item')}] idle {item.get('days_idle')} day(s)")
        lines.append("")

    if report.get("contradictions"):
        lines.append("Contradictions:")
        for c in report["contradictions"]:
            lines.append(
                f"  ⚠ ({c.get('rule')}): ledger says {c.get('ledger_claim')!r}, "
                f"external says {c.get('external_claim')!r}"
            )
        lines.append("")

    lines.append(f"Last activity: {report.get('last_activity_at') or '(none)'}")
    lines.append(f"Recent window: last {report.get('window_workdays')} workday(s)")

    for note in report.get("notes") or []:
        lines.append(f"note: {note}")

    return "\n".join(lines)


def _render_remaining(report: dict) -> str:
    lines = [f"Remaining: {_scope_label(report)}", ""]
    items = report.get("items") or []
    if not items:
        lines.append("(nothing open)")
    for item in items:
        lines.append(_render_remaining_item(item))
    return "\n".join(lines)


def _render_blocked(report: dict) -> str:
    lines = [f"Blocked/waiting: {_scope_label(report)}", ""]
    items = report.get("items") or []
    if not items:
        lines.append("(nothing blocked or waiting)")
    for item in items:
        lines.append(_render_remaining_item(item, with_explanation=True))
    return "\n".join(lines)


def _render_stale(report: dict) -> str:
    lines = [f"Stale (idle >= {report.get('days')} day(s)):", ""]
    items = report.get("items") or []
    if not items:
        lines.append("(nothing stale)")
    for item in items:
        label = f"[{item.get('work_item')}] " if item.get("work_item") else ""
        lines.append(f"  - {label}{item.get('title')} -- idle {item.get('days_idle')} day(s) "
                     f"(last activity {item.get('last_activity_at')})")
    return "\n".join(lines)


def _render_evidence(report: dict) -> str:
    lines = [f"Evidence for {report.get('entry')}:", ""]
    evidence = report.get("evidence") or []
    if not evidence:
        lines.append("(no evidence)")
    for ev in evidence:
        line = f"  - {ev.get('type')}: {ev.get('ref') or '(no ref)'}"
        if ev.get("excerpt"):
            line += f" -- {ev.get('excerpt')}"
        lines.append(line)
        source_event = ev.get("source_event")
        if source_event:
            lines.append(f"      transcript: {source_event.get('transcript_path') or '(unknown)'}")
    return "\n".join(lines)


def _render_inbox(report: dict) -> str:
    lines = ["Inbox (low confidence):", ""]
    activities = report.get("activities") or []
    remaining_items = report.get("remaining_items") or []
    if not activities and not remaining_items:
        lines.append("(inbox empty)")
    if activities:
        lines.append("Activities:")
        for a in activities:
            lines.append(f"  - {a.get('summary')} -- {a.get('confidence_reason')}")
    if remaining_items:
        lines.append("Remaining items:")
        for item in remaining_items:
            lines.append(f"  - {item.get('title')}")
    return "\n".join(lines)
