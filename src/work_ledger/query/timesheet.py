"""Client-facing timesheet export (README "Timesheets").

One row per workday x ticket over a date range, with minutes from the
same merged-interval math as every other report (`query.timespent`).
Client-facing by default: AI-usage fields appear only with
`internal=True`. Optional `round_to` rounds each row's minutes UP to a
billing increment; day and grand totals are then summed from the rounded
rows so the invoice adds up.

Pure query/rendering -- no schema, no stored state. Activities not
attributed to a work item are skipped (a timesheet row needs a ticket);
`worklog day` remains the place unattributed work shows up.
"""
import csv
import io
import math

from work_ledger import timeutil
from work_ledger.query import status as query_status, timespent

_MAX_SUMMARY_CHARS = 200


def _round_up(minutes: int, increment: int) -> int:
    if increment <= 0:
        return minutes
    return int(math.ceil(minutes / increment) * increment)


def timesheet_report(conn, client: str, from_day: str, to_day: str, *,
                     tz: str, rollover_hour: int, round_to: "int | None" = None,
                     internal: bool = False) -> dict:
    """Rows grouped by (workday, ticket), sorted, with day and grand totals."""
    resolved = query_status.resolve_scope(conn, client)
    if resolved["kind"] != "client":
        raise query_status.ScopeNotFound(
            f"timesheet needs a client, but {client!r} is a {resolved['kind']}")

    range_start, _ = timeutil.workday_bounds(from_day, tz, rollover_hour)
    _, range_end = timeutil.workday_bounds(to_day, tz, rollover_hour)

    rows_raw = conn.execute(
        "SELECT a.work_item_id, a.started_at, a.summary,"
        "       wi.external_key, wi.display_id, wi.title,"
        "       wi.ai_usage_score, wi.ai_usage_confidence, wi.ai_usage_reason"
        " FROM activities a JOIN work_items wi ON wi.id = a.work_item_id"
        " WHERE a.deleted_at IS NULL AND wi.deleted_at IS NULL"
        "   AND a.client_id=? AND a.started_at >= ? AND a.started_at < ?"
        " ORDER BY a.started_at",
        (resolved["id"], range_start, range_end)).fetchall()

    buckets: dict = {}
    for row in rows_raw:
        workday = timeutil.workday_of(row["started_at"], tz, rollover_hour)
        key = (workday, row["work_item_id"])
        bucket = buckets.setdefault(key, {
            "workday": workday,
            "work_item_id": row["work_item_id"],
            "ticket": row["external_key"] or row["display_id"],
            "title": row["title"],
            "summaries": [],
            "ai_usage": (None if row["ai_usage_score"] is None else
                         {"score": row["ai_usage_score"],
                          "confidence": row["ai_usage_confidence"],
                          "reason": row["ai_usage_reason"]}),
        })
        if row["summary"] and row["summary"] not in bucket["summaries"]:
            bucket["summaries"].append(row["summary"])

    rows = []
    day_totals: dict = {}
    total = 0
    for (workday, work_item_id), bucket in sorted(
            buckets.items(), key=lambda kv: (kv[0][0], kv[1]["ticket"])):
        day_start, day_end = timeutil.workday_bounds(workday, tz, rollover_hour)
        minutes = timespent.total_minutes(timespent.merge_intervals(
            timespent.intervals_for_work_item(
                conn, work_item_id, start_utc=day_start, end_utc=day_end)))
        summary = "; ".join(bucket["summaries"])[:_MAX_SUMMARY_CHARS]
        out = {
            "workday": workday,
            "ticket": bucket["ticket"],
            "title": bucket["title"],
            "summary": summary,
            "minutes": minutes,
        }
        counted = minutes
        if round_to:
            out["billed_minutes"] = _round_up(minutes, round_to)
            counted = out["billed_minutes"]
        if internal and bucket["ai_usage"] is not None:
            out["ai_usage"] = bucket["ai_usage"]
        rows.append(out)
        day_totals[workday] = day_totals.get(workday, 0) + counted
        total += counted

    return {
        "kind": "timesheet",
        "client": resolved["label"],
        "from": from_day,
        "to": to_day,
        "rounded_to_minutes": round_to,
        "internal": internal,
        "rows": rows,
        "day_totals": day_totals,
        "total_minutes": total,
    }


def _row_minutes(row: dict) -> int:
    return row.get("billed_minutes", row["minutes"])


def render_timesheet(report: dict, *, fmt: str = "md") -> str:
    if fmt == "md":
        return _render_md(report)
    if fmt == "csv":
        return _render_csv(report)
    if fmt == "text":
        return _render_text(report)
    raise ValueError(f"unknown timesheet format: {fmt!r}")


def _render_md(report: dict) -> str:
    internal = report.get("internal")
    header = "| Date | Ticket | Title | Summary | Time |"
    divider = "|---|---|---|---|---|"
    if internal:
        header = header + " AI usage |"
        divider = divider + "---|"
    lines = [f"# Timesheet — {report['client']} ({report['from']} to {report['to']})", "",
             header, divider]
    last_day = None
    for row in report["rows"]:
        if last_day is not None and row["workday"] != last_day:
            lines.append(_md_subtotal(report, last_day, internal))
        last_day = row["workday"]
        cells = [row["workday"], row["ticket"], row["title"],
                 row["summary"], timespent.format_minutes(_row_minutes(row))]
        if internal:
            ai_usage = row.get("ai_usage")
            cells.append("" if ai_usage is None else f"{ai_usage['score']}/5")
        lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |")
    if last_day is not None:
        lines.append(_md_subtotal(report, last_day, internal))
    lines.append("")
    lines.append(f"**Total: {timespent.format_minutes(report['total_minutes'])}**")
    if report.get("rounded_to_minutes"):
        lines.append(f"_Rows rounded up to {report['rounded_to_minutes']}-minute increments._")
    return "\n".join(lines)


def _md_subtotal(report: dict, workday: str, internal: bool) -> str:
    label = timespent.format_minutes(report["day_totals"].get(workday, 0))
    tail = " |" * (1 if internal else 0)
    return f"| **{workday} subtotal** |  |  |  | **{label}** |{tail}"


def _render_csv(report: dict) -> str:
    internal = report.get("internal")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = ["date", "ticket", "title", "summary", "minutes"]
    if internal:
        columns.append("ai_usage_score")
    writer.writerow(columns)
    for row in report["rows"]:
        cells = [row["workday"], row["ticket"], row["title"], row["summary"],
                 _row_minutes(row)]
        if internal:
            ai_usage = row.get("ai_usage")
            cells.append("" if ai_usage is None else ai_usage["score"])
        writer.writerow(cells)
    return buffer.getvalue()


def _render_text(report: dict) -> str:
    lines = [f"Timesheet — {report['client']} ({report['from']} to {report['to']})", ""]
    last_day = None
    for row in report["rows"]:
        if row["workday"] != last_day:
            if last_day is not None:
                lines.append(f"  day total: "
                             f"{timespent.format_minutes(report['day_totals'][last_day])}")
            lines.append(row["workday"])
            last_day = row["workday"]
        lines.append(f"  [{row['ticket']}] {timespent.format_minutes(_row_minutes(row))}"
                     f"  {row['summary'] or row['title']}")
    if last_day is not None:
        lines.append(f"  day total: {timespent.format_minutes(report['day_totals'][last_day])}")
    lines.append("")
    lines.append(f"Total: {timespent.format_minutes(report['total_minutes'])}")
    return "\n".join(lines)
