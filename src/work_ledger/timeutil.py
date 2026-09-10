"""Timestamp formatting, parsing, and workday computation.

Pure stdlib (zoneinfo). No I/O, no imports from any other work_ledger module.
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_DAY_RE = re.compile(
    r"^(?P<month>[a-zA-Z]+)\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{4}))?$"
)


def utc_now_iso() -> str:
    """Current UTC time as YYYY-MM-DDTHH:MM:SS.mmmZ."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> datetime:
    """Parse a UTC ISO-8601 string (with Z or offset) into a tz-aware UTC datetime."""
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt_iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def workday_of(ts_utc_iso: str, tz: str, rollover_hour: int) -> str:
    """Return the YYYY-MM-DD workday that a UTC timestamp belongs to."""
    dt_utc = parse_iso(ts_utc_iso)
    local = dt_utc.astimezone(ZoneInfo(tz))
    if local.hour < rollover_hour:
        local_date = (local - timedelta(days=1)).date()
    else:
        local_date = local.date()
    return local_date.isoformat()


def workday_bounds(workday: str, tz: str, rollover_hour: int) -> tuple[str, str]:
    """Return (inclusive UTC ISO start, exclusive UTC ISO end) for a workday."""
    zone = ZoneInfo(tz)
    year, month, day = (int(p) for p in workday.split("-"))
    start_local = datetime(year, month, day, rollover_hour, 0, 0, tzinfo=zone)
    end_date = start_local.date() + timedelta(days=1)
    end_local = datetime(end_date.year, end_date.month, end_date.day, rollover_hour, 0, 0, tzinfo=zone)
    return _fmt_iso(start_local), _fmt_iso(end_local)


def parse_day_expr(expr: str, tz: str, rollover_hour: int, now=None) -> str:
    """Parse a day expression into a YYYY-MM-DD workday string.

    Accepts: "today", "yesterday", "YYYY-MM-DD", "August 29", "aug 29 2026".
    Raises ValueError for anything else.
    """
    text = expr.strip()
    if not text:
        raise ValueError(f"invalid day expression: {expr!r}")

    if now is None:
        now = datetime.now(timezone.utc)
    current_workday = workday_of(_fmt_iso(now), tz, rollover_hour)

    low = text.lower()
    if low == "today":
        return current_workday
    if low == "yesterday":
        y, m, d = (int(p) for p in current_workday.split("-"))
        return (datetime(y, m, d).date() - timedelta(days=1)).isoformat()

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if m:
        year, month, day = (int(p) for p in m.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            raise ValueError(f"invalid day expression: {expr!r}")

    m = _MONTH_DAY_RE.match(text)
    if m:
        month_name = m.group("month").lower()
        if month_name in _MONTHS:
            month = _MONTHS[month_name]
            day = int(m.group("day"))
            year_str = m.group("year")
            if year_str:
                year = int(year_str)
            else:
                year = int(current_workday.split("-")[0])
            try:
                return datetime(year, month, day).date().isoformat()
            except ValueError:
                raise ValueError(f"invalid day expression: {expr!r}")

    raise ValueError(f"invalid day expression: {expr!r}")
