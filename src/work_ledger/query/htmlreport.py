"""Self-contained HTML reports (README "HTML reports").

`build_data` composes the existing query layers (range/day tree with
minutes, AI usage, Claude cost; blockers via status) for a period;
`render_html` turns it into ONE file with inline CSS and inline SVG --
zero external requests, works offline, safe to archive or forward.

Client-facing rule carries over: AI-usage and cost fields exist in the
data and the page only with `internal=True`.

Chart conventions follow the data-viz method: a single-series bar chart
(sequential blue, no legend -- the title names the series), thin marks
with rounded data-ends anchored to the baseline, native SVG tooltips,
stat tiles for the headline numbers, and the per-client tables as the
accessible table view. Dark mode is selected (its own steps), not an
automatic flip. Every user-derived string is HTML-escaped.
"""
import html as _html
from datetime import date, timedelta

from work_ledger import timeutil
from work_ledger.query import daily as query_daily
from work_ledger.query import status as query_status
from work_ledger.query import timespent
from work_ledger.query import week as query_week

PERIODS = ("today", "week", "month")


def _period_bounds(period: str, today: str) -> "tuple[str, str]":
    if period == "today":
        return today, today
    if period == "week":
        return query_week.monday_of(today), today
    if period == "month":
        return date.fromisoformat(today).replace(day=1).isoformat(), today
    raise ValueError(f"unknown period {period!r}; expected one of {PERIODS}")


def _workdays(start: str, end: str) -> "list[str]":
    day = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    days = []
    while day <= stop:
        days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def _strip_internal(node) -> None:
    """Remove AI/cost fields in place (public reports)."""
    if isinstance(node, dict):
        for key in ("ai_usage", "claude"):
            node.pop(key, None)
        for value in node.values():
            _strip_internal(value)
    elif isinstance(node, list):
        for value in node:
            _strip_internal(value)


def _annotate_activity_times(base: dict, *, tz: str, rollover_hour: int) -> None:
    """Tag each activity (in place) with its own `workday` and `minutes` --
    the user's `stated_duration_minutes` if given, else its own started/ended
    span, rounded. Distinct from a work item's aggregate `minutes`, which
    merges intervals across all of that item's activities and stays the
    authoritative per-item total; this is just per-activity display detail.
    """
    for client_node in base.get("clients") or []:
        for project_node in client_node.get("projects") or []:
            for node in project_node.get("work_items") or []:
                for activity in node.get("activities") or []:
                    started_at = activity.get("started_at")
                    activity["workday"] = (
                        timeutil.workday_of(started_at, tz, rollover_hour)
                        if started_at else None)
                    stated = activity.get("stated_duration_minutes")
                    if stated:
                        activity["minutes"] = stated
                    elif started_at and activity.get("ended_at"):
                        delta = (timeutil.parse_iso(activity["ended_at"])
                                - timeutil.parse_iso(started_at))
                        activity["minutes"] = max(0, round(delta.total_seconds() / 60))
                    else:
                        activity["minutes"] = 0


def build_data(conn, scope: "str | None", *, period: str = "today",
               from_day: "str | None" = None, to_day: "str | None" = None,
               tz: str, rollover_hour: int, internal: bool = False) -> dict:
    """The report's full data set for a period or explicit workday range."""
    resolved = query_status.resolve_scope(conn, scope)
    if resolved["kind"] not in ("all", "client"):
        raise query_status.ScopeNotFound(
            f"report takes a client (or nothing), but {scope!r}"
            f" is a {resolved['kind']}")
    client_id = resolved["id"] if resolved["kind"] == "client" else None

    today = timeutil.workday_of(timeutil.utc_now_iso(), tz, rollover_hour)
    if from_day or to_day:
        start, end = from_day or to_day, to_day or from_day
        label = f"{start} to {end}"
    else:
        start, end = _period_bounds(period, today)
        label = period

    base = query_daily.range_report(conn, start, end, tz=tz,
                                    rollover_hour=rollover_hour,
                                    client_id=client_id)
    _annotate_activity_times(base, tz=tz, rollover_hour=rollover_hour)

    days = []
    for workday in _workdays(start, end):
        day_start, day_end = timeutil.workday_bounds(workday, tz, rollover_hour)
        intervals = []
        for client_node in base.get("clients") or []:
            for project_node in client_node.get("projects") or []:
                for node in project_node.get("work_items") or []:
                    if node.get("work_item_id") is None:
                        continue
                    intervals.extend(timespent.intervals_for_work_item(
                        conn, node["work_item_id"],
                        start_utc=day_start, end_utc=day_end))
        days.append({"workday": workday,
                     "minutes": timespent.total_minutes(
                         timespent.merge_intervals(intervals))})

    status = query_status.status_report(conn, scope, tz=tz,
                                        rollover_hour=rollover_hour)
    blockers = []
    for item in (status.get("blockers") or []) + (status.get("waiting") or []):
        blockers.append({"title": item.get("title"),
                         "state": item.get("state"),
                         "explanation": item.get("explanation")})

    total_cost = None
    if internal:
        costs = [c["claude"]["cost_usd"] for c in base.get("clients") or []
                 if c.get("claude") and c["claude"].get("cost_usd") is not None]
        total_cost = round(sum(costs), 2) if costs else None

    data = {
        "kind": "html_report",
        "scope": resolved["label"],
        "period": label,
        "start_workday": start,
        "end_workday": end,
        "internal": internal,
        "days": days,
        "clients": base.get("clients") or [],
        "blockers": blockers,
        "total_minutes": sum(d["minutes"] for d in days),
        "total_cost_usd": total_cost,
        "contradiction_count": sum(
            len(node.get("contradictions") or [])
            for c in base.get("clients") or []
            for p in c.get("projects") or []
            for node in p.get("work_items") or []),
    }
    if not internal:
        _strip_internal(data)
    return data


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _e(value) -> str:
    return _html.escape(str(value if value is not None else ""), quote=True)


def _bar_geometry(days: "list[dict]", *, width: int, height: int) -> "list[dict]":
    """x/y/w/h per day, linear scale from zero, 2px gaps, capped bar width."""
    n = len(days)
    if n == 0:
        return []
    peak = max((d["minutes"] for d in days), default=0) or 1
    gap = 2
    bar_width = min(48, max(6, (width - gap * (n - 1)) // n))
    bars = []
    for index, day in enumerate(days):
        bar_height = round((day["minutes"] / peak) * (height - 4), 1)
        bars.append({
            "workday": day["workday"],
            "minutes": day["minutes"],
            "x": index * (bar_width + gap),
            "y": round(height - bar_height, 1),
            "w": bar_width,
            "h": bar_height,
        })
    return bars


def _svg_chart(days: "list[dict]") -> str:
    width, height, label_band, top_margin = 560, 120, 18, 14
    bars = _bar_geometry(days, width=width, height=height)
    if not bars or all(b["minutes"] == 0 for b in bars):
        return ""
    baseline = height + top_margin
    parts = [f'<svg class="chart" viewBox="0 0 {width} {baseline + label_band}" '
             f'role="img" aria-label="Minutes per day" '
             f'preserveAspectRatio="xMinYMax meet">']
    parts.append(f'<line x1="0" y1="{baseline}" x2="{width}" y2="{baseline}" '
                 f'class="axis"/>')
    show_every = max(1, len(bars) // 8)
    for index, bar in enumerate(bars):
        if bar["h"] > 0:
            # Rounded data-end anchored to the baseline: round only the top.
            radius = min(4, bar["w"] / 2, bar["h"])
            parts.append(
                f'<path class="bar" d="M{bar["x"]},{baseline} '
                f'v-{max(bar["h"] - radius, 0):.1f} '
                f'q0,-{radius} {radius},-{radius} h{bar["w"] - 2 * radius} '
                f'q{radius},0 {radius},{radius} v{max(bar["h"] - radius, 0):.1f} z">'
                f'<title>{_e(bar["workday"])}: '
                f'{_e(timespent.format_minutes(bar["minutes"]))}</title></path>')
            # Visible value label above the bar -- the <title> above is a
            # hover-only tooltip, invisible in a static/printed view.
            label_y = max(bar["y"] + top_margin - 4, 9)
            parts.append(
                f'<text class="value" x="{bar["x"] + bar["w"] / 2}" y="{label_y}" '
                f'text-anchor="middle">'
                f'{_e(timespent.format_minutes(bar["minutes"]))}</text>')
        if index % show_every == 0:
            parts.append(
                f'<text class="tick" x="{bar["x"] + bar["w"] / 2}" '
                f'y="{baseline + 13}" text-anchor="middle">'
                f'{_e(bar["workday"][5:])}</text>')
    parts.append("</svg>")
    return "".join(parts)


_STYLE = """
:root { color-scheme: light dark; }
body {
  margin: 0 auto; padding: 24px; background: #fcfcfb; color: #0b0b0b;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  max-width: 75%; box-sizing: border-box;
}
.muted { color: #52514e; }
h1 { font-size: 20px; margin: 0 0 2px; }
h2 { font-size: 16px; margin: 28px 0 8px; }
.tiles { display: flex; gap: 12px; flex-wrap: wrap; margin: 18px 0; }
.tile {
  background: #f0efec; border-radius: 8px; padding: 12px 18px; min-width: 110px;
}
.tile .value { font-size: 22px; font-weight: 600; }
.tile .label { font-size: 12px; color: #52514e; }
.chart { max-width: 100%; width: 100%; display: block; margin: 8px 0 4px; }
.chart .bar { fill: #2a78d6; }
.chart .axis { stroke: #d8d7d2; stroke-width: 1; }
.chart .tick { fill: #52514e; font-size: 9px; }
.chart .value { fill: #0b0b0b; font-size: 10px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; max-width: 100%; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #e5e4e0; vertical-align: top; }
th { font-size: 12px; color: #52514e; font-weight: 600; }
td.num, th.num { text-align: right; white-space: nowrap; }
td .summaries { margin: 0; padding-left: 16px; }
td .summaries li { margin: 3px 0; }
td .summaries li:first-child { margin-top: 0; }
td .summaries li:last-child { margin-bottom: 0; }
td .summaries .when { color: #52514e; font-size: 12px; white-space: nowrap; }
.badge {
  display: inline-block; padding: 1px 7px; border-radius: 9px;
  background: #f0efec; font-size: 12px;
}
.blockers li { margin: 4px 0; }
footer { margin-top: 32px; font-size: 12px; color: #52514e; }
@media (prefers-color-scheme: dark) {
  body { background: #1a1a19; color: #ffffff; }
  .muted, .tile .label, th, .blockers .why, footer, td .summaries .when { color: #c3c2b7; }
  .tile, .badge { background: #262624; }
  th, td { border-bottom-color: #33322f; }
  .chart .bar { fill: #3987e5; }
  .chart .axis { stroke: #3a3936; }
  .chart .tick { fill: #c3c2b7; }
  .chart .value { fill: #ffffff; }
}
"""


def render_html(data: dict) -> str:
    internal = data.get("internal")
    tiles = [("Time", timespent.format_minutes(data["total_minutes"]))]
    if internal and data.get("total_cost_usd") is not None:
        tiles.append(("Claude cost", f"${data['total_cost_usd']:.2f}"))
    ticket_count = sum(
        1 for c in data["clients"] for p in c.get("projects") or []
        for _ in p.get("work_items") or [])
    tiles.append(("Tickets touched", str(ticket_count)))
    if data["blockers"]:
        tiles.append(("Blocked/waiting", str(len(data["blockers"]))))
    if data["contradiction_count"]:
        tiles.append(("Contradictions", str(data["contradiction_count"])))

    out = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Work Ledger — {_e(data['scope'])} — {_e(data['period'])}</title>",
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>Work Ledger — {_e(data['scope'])}</h1>",
        f'<div class="muted">{_e(data["start_workday"])} to {_e(data["end_workday"])}'
        + (" · internal (AI &amp; cost shown)" if internal else "") + "</div>",
        '<div class="tiles">' + "".join(
            f'<div class="tile"><div class="value">{_e(value)}</div>'
            f'<div class="label">{_e(label)}</div></div>'
            for label, value in tiles) + "</div>",
    ]

    chart = _svg_chart(data["days"])
    if chart and len(data["days"]) > 1:
        out.append("<h2>Minutes per day</h2>")
        out.append(chart)

    for client_node in data["clients"]:
        client_minutes = timespent.format_minutes(client_node.get("minutes") or 0)
        out.append(f"<h2>{_e(client_node.get('client'))} "
                   f'<span class="muted">— {_e(client_minutes)}</span></h2>')
        claude = client_node.get("claude")
        if internal and claude:
            from work_ledger.query import tokencost
            totals = claude.get("totals") or {}
            cost = claude.get("cost_usd")
            cost_text = f" (~${cost:.2f})" if cost is not None else ""
            out.append(f'<div class="muted">Claude: '
                       f'{_e(tokencost.format_tokens(totals.get("input_tokens", 0)))} in / '
                       f'{_e(tokencost.format_tokens(totals.get("output_tokens", 0)))} out'
                       f'{_e(cost_text)}</div>')
        header = "<tr><th>Ticket</th><th>Title</th><th>Summary</th>"
        if internal:
            header += "<th>AI usage</th>"
        header += '<th class="num">Time</th></tr>'
        rows = [header]
        for project_node in client_node.get("projects") or []:
            for node in project_node.get("work_items") or []:
                # A list, not a semicolon-joined blob: several activities'
                # summaries read as one unreadable run-on line joined that way.
                # Kept first-seen by summary text, same as summaries.dedupe,
                # but keeping the activity attached so its own date/time can
                # be shown alongside -- not just the work item's aggregate.
                seen_summaries = set()
                picked_activities = []
                for activity in node.get("activities") or []:
                    text = activity.get("summary")
                    if text and text not in seen_summaries:
                        seen_summaries.add(text)
                        picked_activities.append(activity)
                    if len(picked_activities) >= 5:
                        break
                summary_html = "<ul class=\"summaries\">" + "".join(
                    f'<li><span class="when">{_e(a.get("workday"))} · '
                    f'{_e(timespent.format_minutes(a.get("minutes") or 0))}</span> '
                    f'{_e(a.get("summary"))}</li>'
                    for a in picked_activities) + "</ul>"
                cells = (f"<tr><td>{_e(node.get('work_item'))}</td>"
                         f"<td>{_e(node.get('title'))}</td>"
                         f"<td>{summary_html}</td>")
                if internal:
                    ai_usage = node.get("ai_usage")
                    badge = (f'<span class="badge">{_e(ai_usage["score"])}/5</span>'
                             if ai_usage else "")
                    cells += f"<td>{badge}</td>"
                cells += (f'<td class="num">'
                          f'{_e(timespent.format_minutes(node.get("minutes") or 0))}'
                          f"</td></tr>")
                rows.append(cells)
        out.append("<table>" + "".join(rows) + "</table>")

    if data["blockers"]:
        out.append("<h2>Blocked &amp; waiting</h2><ul class=\"blockers\">")
        for blocker in data["blockers"]:
            why = (f' <span class="why muted">— {_e(blocker["explanation"])}</span>'
                   if blocker.get("explanation") else "")
            out.append(f"<li>{_e(blocker['title'])} "
                       f'<span class="badge">{_e(blocker["state"])}</span>{why}</li>')
        out.append("</ul>")

    from work_ledger import __version__, timeutil
    out.append(f"<footer>Generated {_e(timeutil.utc_now_iso())} · "
               f"Work Ledger {_e(__version__)} · data stays local</footer>")
    out.append("</body></html>")
    return "\n".join(out)
