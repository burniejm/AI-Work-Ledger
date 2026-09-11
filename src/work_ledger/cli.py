"""CLI dispatcher for the worklog command.

argparse owns dispatch: there is no argv[0] in _HANDLERS pre-check. Every
subcommand's shared flags (currently --format) are attached via
parents=[common_parser()] on each subparser (Ruling 1), never on the
top-level parser, so `worklog <command> --format json` parses correctly.
"""
import argparse
import json
import os
import pathlib
import sys
from datetime import date, timedelta
from typing import Callable

from work_ledger import __version__, SCHEMA_VERSION
from work_ledger import backup as backup_mod
from work_ledger import config as config_mod
from work_ledger import connections as connections_mod
from work_ledger import contradictions as contradictions_mod
from work_ledger import corrections, entities, manual, timeutil
from work_ledger import doctor as doctor_mod
from work_ledger import hooks_runtime
from work_ledger import paths, redact
from work_ledger import setupwizard
from work_ledger import uninstall as uninstall_mod
from work_ledger.capture import sessions as capture_sessions
from work_ledger.db import connection as dbc
from work_ledger.db import migrations
from work_ledger.jira import mcp as jira_mcp
from work_ledger.jira import refresh as jira_refresh
from work_ledger.query import daily as query_daily
from work_ledger.query import render as query_render
from work_ledger.query import scoped as query_scoped
from work_ledger.query import status as query_status

_HANDLERS: "dict[str, Callable[[argparse.Namespace], int]]" = {}
_PARSER_SETUPS: "dict[str, Callable[[argparse.ArgumentParser], None]]" = {}


def register(name: str, setup=None):
    def deco(fn):
        _HANDLERS[name] = fn
        if setup:
            _PARSER_SETUPS[name] = setup
        return fn
    return deco


def registered_names() -> "list[str]":
    return sorted(_HANDLERS)


def common_parser() -> argparse.ArgumentParser:
    """Flags shared by every subcommand. Attached with parents=[...] so argparse
    accepts them AFTER the subcommand name (Ruling 1)."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--format", choices=("text", "json"), default="text")
    return p


def build_parser() -> argparse.ArgumentParser:
    common = common_parser()
    p = argparse.ArgumentParser(prog="worklog")
    sub = p.add_subparsers(dest="command")
    for name in sorted(_HANDLERS):
        # `export` defines its own `--format` (json/markdown/csv: an output
        # format, not text-vs-json report rendering) rather than inheriting
        # the shared one from `common` -- skipping `parents=[common]` here
        # avoids colliding with it. (conflict_handler="resolve" was tried and
        # rejected: parents copies action *references*, so resolving a
        # conflict on one subparser calls `_remove_action` on the shared
        # `common` parser itself, silently deleting --format for every
        # subcommand built afterwards.)
        # `export` and `timesheet` define their own `--format` (output styles
        # beyond text/json) rather than inheriting the shared one.
        if name in ("export", "timesheet", "week"):
            sp = sub.add_parser(name)
        else:
            sp = sub.add_parser(name, parents=[common])
        if name in _PARSER_SETUPS:
            _PARSER_SETUPS[name](sp)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)          # argparse owns dispatch and validation
    except SystemExit as exc:                    # unknown subcommand / bad flag
        return 2 if exc.code else 0
    if not getattr(args, "command", None):
        parser.print_usage(sys.stderr)
        return 2
    try:
        return _HANDLERS[args.command](args)
    except Exception as exc:  # never traceback at the user
        print(f"worklog: {exc}", file=sys.stderr)
        return 1


@register("version")
def _cmd_version(args: argparse.Namespace) -> int:
    print(f"worklog {__version__} (schema {SCHEMA_VERSION})")
    return 0


def _redact_secrets(node):
    """Deep-copy node, replacing any string that looks like a secret with
    '<redacted>' (Global Constraints: secrets never in diagnostic output).
    Uses redact.looks_like_secret — the sole owner of credential-pattern
    knowledge (Ruling 22, DESIGN §3)."""
    if isinstance(node, dict):
        return {k: _redact_secrets(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_redact_secrets(v) for v in node]
    if redact.looks_like_secret(node):
        return "<redacted>"
    return node


def _format_config_text(value, indent=0) -> "list[str]":
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        if not value:
            return [f"{pad}{{}}"]
        for key, val in value.items():
            if isinstance(val, (dict, list)) and val:
                lines.append(f"{pad}{key}:")
                lines.extend(_format_config_text(val, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_format_config_scalar(val)}")
    elif isinstance(value, list):
        if not value:
            return [f"{pad}[]"]
        for item in value:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}-")
                lines.extend(_format_config_text(item, indent + 1))
            else:
                lines.append(f"{pad}- {_format_config_scalar(item)}")
    return lines


def _format_config_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _setup_config(sp: argparse.ArgumentParser) -> None:
    common = common_parser()
    sub = sp.add_subparsers(dest="action")
    sub.add_parser("show", parents=[common])
    sub.add_parser("path", parents=[common])
    sub.add_parser("validate", parents=[common])


@register("config", setup=_setup_config)
def _cmd_config(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None) or "show"

    if action == "path":
        print(paths.config_path())
        return 0

    if action == "validate":
        cfg = config_mod.load()
        problems = config_mod.validate(cfg)
        if args.format == "json":
            print(json.dumps({"problems": problems}, indent=2))
        else:
            for problem in problems:
                print(problem)
        return 1 if problems else 0

    if action == "show":
        cfg = _redact_secrets(config_mod.load())
        if args.format == "json":
            print(json.dumps(cfg, indent=2))
        else:
            print("\n".join(_format_config_text(cfg)))
        return 0

    print(f"worklog: unknown config action: {action}", file=sys.stderr)
    return 2


def _setup_migrate(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--status", action="store_true")


@register("migrate", setup=_setup_migrate)
def _cmd_migrate(args: argparse.Namespace) -> int:
    conn = dbc.connect()
    try:
        if args.status:
            done = migrations.applied(conn)
            discovered = {version: (name, sql) for version, name, sql in migrations.discover()}
            versions = sorted(set(done) | set(discovered))
            rows = [
                {
                    "version": v,
                    "name": done[v]["name"] if v in done else discovered[v][0],
                    "status": "applied" if v in done else "pending",
                }
                for v in versions
            ]
            if args.format == "json":
                print(json.dumps(rows, indent=2))
            else:
                for row in rows:
                    print(f"{row['version']:03d}  {row['name']:<40} {row['status']}")
            return 0

        applied_versions = migrations.apply_all(conn)
        if args.format == "json":
            print(json.dumps({"applied": applied_versions}, indent=2))
        elif applied_versions:
            print("applied: " + ", ".join(str(v) for v in applied_versions))
        else:
            print("up to date")
        return 0
    finally:
        conn.close()


def _setup_hook(sp: argparse.ArgumentParser) -> None:
    # nargs='?' (not required), same reasoning as `_setup_day`: the Ruling 1
    # contract test parses every subcommand with no other args.
    sp.add_argument("event", nargs="?")


@register("hook", setup=_setup_hook)
def _cmd_hook(args: argparse.Namespace) -> int:
    """`worklog hook <event>` -- the ONLY hook entry point (Ruling 26):
    hooks/hooks.json invokes this directly, no shim script. Never writes to
    stdout, never raises, never returns non-zero -- `bin/worklog` turns the
    return value into the process exit code, and it is always 0 here."""
    # First statement: bail before touching config, the database, or stdin.
    if os.environ.get("WORK_LEDGER_NESTED"):
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        hooks_runtime.log_exception(exc, f"hook:{args.event}:stdin-parse")
        return 0
    hooks_runtime.handle(args.event, payload)
    return 0


def _resolve_ticket(conn, external_key: str) -> str:
    row = conn.execute(
        "SELECT id FROM work_items WHERE external_key=? AND deleted_at IS NULL "
        "ORDER BY created_at LIMIT 1",
        (external_key,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no work item found for ticket {external_key!r}")
    return row["id"]


def _resolve_client(conn, name: str) -> str:
    row = entities.get_client_by_name(conn, name)
    if row is None:
        raise ValueError(f"no client found named {name!r}")
    return row["id"]


def _activity_client_id(conn, activity_id: str):
    row = conn.execute("SELECT client_id FROM activities WHERE id=?", (activity_id,)).fetchone()
    return row["client_id"] if row is not None else None


def _resolve_project(conn, client_id: str, name: str) -> str:
    slug = entities.slugify(name)
    row = conn.execute(
        "SELECT id FROM projects WHERE client_id IS ? AND slug=? AND deleted_at IS NULL",
        (client_id, slug),
    ).fetchone()
    if row is None:
        raise ValueError(f"no project found named {name!r}")
    return row["id"]


def _setup_correct(sp: argparse.ArgumentParser) -> None:
    common = common_parser()
    sub = sp.add_subparsers(dest="action")

    mv = sub.add_parser("move-activity", parents=[common])
    mv.add_argument("--activity", required=True)
    target = mv.add_mutually_exclusive_group()
    target.add_argument("--to-ticket")
    target.add_argument("--to-work-item")
    mv.add_argument("--client")
    mv.add_argument("--project")
    mv.add_argument("--reason", required=True)
    mv.add_argument("--learn-alias", action="store_true")

    st = sub.add_parser("set", parents=[common])
    st.add_argument("--table", required=True)
    st.add_argument("--id", required=True)
    st.add_argument("--field", required=True)
    st.add_argument("--value", required=True)
    st.add_argument("--reason", required=True)

    de = sub.add_parser("delete", parents=[common])
    de.add_argument("--table", required=True)
    de.add_argument("--id", required=True)
    de.add_argument("--reason", required=True)

    un = sub.add_parser("undo", parents=[common])
    un.add_argument("--group", required=True)

    hi = sub.add_parser("history", parents=[common])
    hi.add_argument("--table", required=True)
    hi.add_argument("--id", required=True)


@register("correct", setup=_setup_correct)
def _cmd_correct(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None)
    if not action:
        print("worklog: correct requires an action (move-activity|set|delete|undo|history)", file=sys.stderr)
        return 2

    conn = dbc.connect()
    try:
        if action == "move-activity":
            work_item_id = args.to_work_item
            if args.to_ticket:
                work_item_id = _resolve_ticket(conn, args.to_ticket)
            client_id = _resolve_client(conn, args.client) if args.client else None
            project_id = None
            if args.project:
                # --project alone (no --client) resolves within the
                # activity's *current* client, not client_id=NULL --
                # otherwise a real project under a real client can never
                # match `WHERE client_id IS NULL`.
                scope_client_id = client_id
                if scope_client_id is None:
                    scope_client_id = _activity_client_id(conn, args.activity)
                project_id = _resolve_project(conn, scope_client_id, args.project)
            group_id = corrections.move_activity(
                conn, args.activity, work_item_id=work_item_id, client_id=client_id,
                project_id=project_id, reason=args.reason, learn_alias=args.learn_alias,
            )
        elif action == "set":
            group_id = corrections.apply_field_update(
                conn, args.table, args.id, args.field, args.value, reason=args.reason,
            )
        elif action == "delete":
            group_id = corrections.soft_delete_record(conn, args.table, args.id, reason=args.reason)
        elif action == "undo":
            group_id = corrections.undo(conn, args.group)
        elif action == "history":
            rows = [dict(r) for r in corrections.history(conn, args.table, args.id)]
            if args.format == "json":
                print(json.dumps(rows, indent=2))
            else:
                for r in rows:
                    print(f"{r['applied_at']}  {r['operation']:<12} {r['field'] or '-':<24} "
                          f"{r['old_value_json']} -> {r['new_value_json']}  ({r['reason'] or ''})")
            return 0
        else:
            print(f"worklog: unknown correct action: {action}", file=sys.stderr)
            return 2

        if args.format == "json":
            print(json.dumps({"correction_group_id": group_id}, indent=2))
        else:
            print(group_id)
        return 0
    finally:
        conn.close()


def _setup_add(sp: argparse.ArgumentParser) -> None:
    # REMAINDER: everything after `add` (and after --format, which the
    # parents=[common_parser()] parent consumes first) is the raw statement
    # text -- reconstructed with a single space between tokens, so
    # `worklog add client=Acme Fixed the thing` works without quoting.
    sp.add_argument("text", nargs=argparse.REMAINDER)


@register("add", setup=_setup_add)
def _cmd_add(args: argparse.Namespace) -> int:
    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text.strip():
        print("worklog: add requires text (as argv or on stdin)", file=sys.stderr)
        return 2

    statement = manual.parse_add(text)
    conn = dbc.connect()
    try:
        activity_id = manual.record(conn, statement)
        row = conn.execute(
            "SELECT wi.id AS work_item_id, wi.display_id, wi.external_key, wi.title, "
            "c.name AS client_name, p.name AS project_name "
            "FROM activities a "
            "LEFT JOIN work_items wi ON wi.id = a.work_item_id "
            "LEFT JOIN clients c ON c.id = a.client_id "
            "LEFT JOIN projects p ON p.id = a.project_id "
            "WHERE a.id = ?",
            (activity_id,),
        ).fetchone()

        work_item = None
        client_name = None
        project_name = None
        if row is not None:
            client_name = row["client_name"]
            project_name = row["project_name"]
            if row["work_item_id"] is not None:
                work_item = {
                    "id": row["work_item_id"],
                    "display_id": row["display_id"],
                    "external_key": row["external_key"],
                    "title": row["title"],
                }

        if args.format == "json":
            print(json.dumps({
                "activity_id": activity_id,
                "work_item": work_item,
                "client": client_name,
                "project": project_name,
            }, indent=2))
        else:
            print(activity_id)
        return 0
    finally:
        conn.close()


def _tz_and_rollover() -> "tuple[str, int]":
    return config_mod.get("timezone", "America/Chicago"), config_mod.get("rollover_hour", 3)


def _setup_report_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--evidence", action="store_true", dest="include_evidence")
    sp.add_argument("--include-inbox", action="store_true")


def _sweep_stale_sessions(conn) -> None:
    """DESIGN gap G1: finalize sessions left idle past
    `session.idle_finalize_minutes`. `SessionEnd` is not guaranteed to fire,
    so nothing runs on a timer -- the sweep rides SessionStart *and* every
    read path (`worklog doctor` and every report), which is what makes a
    report about "today" describe closed sessions rather than a phantom
    live one.

    Best-effort and exception-safe by design: this is bookkeeping the user
    did not ask for, so a locked database, an un-migrated schema, or any
    other failure must never break the report they did ask for.
    """
    try:
        capture_sessions.finalize_stale(
            conn, now=timeutil.utc_now_iso(),
            idle_minutes=capture_sessions.idle_finalize_minutes(),
        )
    except Exception:
        pass


def _report_conn():
    """A connection for a read-only report path, with the G1 sweep already
    applied. Every report command opens its connection through here."""
    conn = dbc.connect()
    _sweep_stale_sessions(conn)
    return conn


def _print_report(report: dict, fmt: str) -> None:
    print(query_render.render_report(report, fmt=fmt))


@register("today", setup=_setup_report_flags)
def _cmd_today(args: argparse.Namespace) -> int:
    tz, rollover_hour = _tz_and_rollover()
    workday = timeutil.parse_day_expr("today", tz, rollover_hour)
    conn = _report_conn()
    try:
        report = query_daily.day_report(
            conn, workday, tz=tz, rollover_hour=rollover_hour,
            include_evidence=args.include_evidence, include_inbox=args.include_inbox,
        )
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_day(sp: argparse.ArgumentParser) -> None:
    # nargs='?' (not required): Ruling 1's contract test parses every
    # registered subcommand as `[name, "--format", "json"]` with no other
    # args, so a missing `expr` must fail at runtime, not at parse time.
    sp.add_argument("expr", nargs="?")
    _setup_report_flags(sp)


@register("day", setup=_setup_day)
def _cmd_day(args: argparse.Namespace) -> int:
    if not args.expr:
        print("worklog: day requires a day expression (e.g. today, yesterday, 2026-08-29)", file=sys.stderr)
        return 2
    tz, rollover_hour = _tz_and_rollover()
    workday = timeutil.parse_day_expr(args.expr, tz, rollover_hour)
    conn = _report_conn()
    try:
        report = query_daily.day_report(
            conn, workday, tz=tz, rollover_hour=rollover_hour,
            include_evidence=args.include_evidence, include_inbox=args.include_inbox,
        )
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_range(sp: argparse.ArgumentParser) -> None:
    # Both positional and optional for the same reason as `_setup_day`.
    sp.add_argument("start", nargs="?")
    sp.add_argument("end", nargs="?")
    _setup_report_flags(sp)


@register("range", setup=_setup_range)
def _cmd_range(args: argparse.Namespace) -> int:
    if not args.start or not args.end:
        print("worklog: range requires <start> <end> day expressions", file=sys.stderr)
        return 2
    tz, rollover_hour = _tz_and_rollover()
    start_workday = timeutil.parse_day_expr(args.start, tz, rollover_hour)
    end_workday = timeutil.parse_day_expr(args.end, tz, rollover_hour)
    conn = _report_conn()
    try:
        report = query_daily.range_report(
            conn, start_workday, end_workday, tz=tz, rollover_hour=rollover_hour,
            include_evidence=args.include_evidence, include_inbox=args.include_inbox,
        )
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_status(sp: argparse.ArgumentParser) -> None:
    # nargs='?' (not required), same reasoning as `_setup_day`: the Ruling 1
    # contract test parses every subcommand with no other args.
    sp.add_argument("scope", nargs="?")


@register("status", setup=_setup_status)
def _cmd_status(args: argparse.Namespace) -> int:
    tz, rollover_hour = _tz_and_rollover()
    workdays = config_mod.get("reports.status_workdays", 7)
    conn = _report_conn()
    try:
        report = query_status.status_report(
            conn, args.scope, tz=tz, rollover_hour=rollover_hour, workdays=workdays,
        )
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_remaining(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("scope", nargs="?")
    sp.add_argument("--state", dest="state_filter")
    sp.add_argument("--include-inbox", action="store_true")


@register("remaining", setup=_setup_remaining)
def _cmd_remaining(args: argparse.Namespace) -> int:
    conn = _report_conn()
    try:
        report = query_scoped.remaining_report(
            conn, args.scope, state_filter=args.state_filter, include_inbox=args.include_inbox,
        )
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_blocked(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("scope", nargs="?")


@register("blocked", setup=_setup_blocked)
def _cmd_blocked(args: argparse.Namespace) -> int:
    conn = _report_conn()
    try:
        report = query_scoped.blocked_report(conn, args.scope)
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_stale(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--days", type=int, default=None)


@register("stale", setup=_setup_stale)
def _cmd_stale(args: argparse.Namespace) -> int:
    tz, rollover_hour = _tz_and_rollover()
    days = args.days if args.days is not None else config_mod.get("reports.stale_after_days", 14)
    conn = _report_conn()
    try:
        report = query_scoped.stale_report(conn, days=days, tz=tz, rollover_hour=rollover_hour)
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


# How far back an activity still counts a client as "open" in `worklog
# context` -- long enough that a client worked on last month is still
# offered as a scope, short enough that a finished engagement drops off.
CONTEXT_ACTIVITY_WINDOW_DAYS = 30


def _context_open_clients(conn, *, tz: str, rollover_hour: int, today: str) -> "list[str]":
    """Client names worth offering as a scope: those with a non-terminal work
    item, or with any activity inside the recent window. One query, index-
    friendly, no joins on the activity side beyond the id set."""
    window_start = (
        date.fromisoformat(today) - timedelta(days=CONTEXT_ACTIVITY_WINDOW_DAYS)
    ).isoformat()
    cutoff, _ = timeutil.workday_bounds(window_start, tz, rollover_hour)
    rows = conn.execute(
        "SELECT name FROM clients WHERE deleted_at IS NULL AND id IN ("
        "  SELECT client_id FROM work_items"
        "   WHERE deleted_at IS NULL AND client_id IS NOT NULL"
        "     AND local_state NOT IN ('completed','cancelled')"
        "     AND (jira_status_category IS NULL OR jira_status_category <> 'done')"
        "  UNION"
        "  SELECT client_id FROM activities"
        "   WHERE deleted_at IS NULL AND client_id IS NOT NULL AND in_inbox=0"
        "     AND started_at >= ?"
        ") ORDER BY name",
        (cutoff,),
    ).fetchall()
    return [row["name"] for row in rows]


def _context_stale_connections(conn) -> "list[str]":
    """Connections whose Jira data the model should treat as stale: an MCP
    connection with at least one stale issue key (exactly what
    `worklog jira plan-refresh` would go after -- asked here with limit=1, so
    it costs one row per connection), plus any enabled connection whose
    health is not `healthy`. Nothing here contacts Jira."""
    names = set()
    plan = jira_mcp.plan_refresh(conn, connection_name=None, limit=1)
    for entry in plan["connections"]:
        names.add(entry["connection"])
    for row in connections_mod.list_connections(conn):
        if row["health_state"] != "healthy":
            names.add(row["name"])
    return sorted(names)


def _context_from_ledger(conn, *, tz: str, rollover_hour: int, today: str) -> dict:
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM contradictions WHERE status='open' AND deleted_at IS NULL"
    ).fetchone()["n"]
    inbox = conn.execute(
        "SELECT (SELECT COUNT(*) FROM activities WHERE in_inbox=1 AND deleted_at IS NULL)"
        "     + (SELECT COUNT(*) FROM remaining_items WHERE in_inbox=1 AND deleted_at IS NULL)"
        "   AS n"
    ).fetchone()["n"]
    return {
        "open_clients": _context_open_clients(conn, tz=tz, rollover_hour=rollover_hour, today=today),
        "pending_contradictions": int(pending),
        "inbox_count": int(inbox),
        "stale_connections": _context_stale_connections(conn),
    }


@register("context")
def _cmd_context(args: argparse.Namespace) -> int:
    """`worklog context [--format json]` -- the cheap orientation preamble the
    `worklog` skill runs inline before the model sees its instructions
    (DESIGN §4.4, Task 37).

    Deliberately tiny: four counting queries, no Jira call, no Stage B, no
    stale-session sweep. It runs on *every* skill invocation, so its cost is
    a fixed tax on the entire natural-language surface.

    It also never fails loudly. The skill's preamble is
    `... context --format json 2>/dev/null || true`, so a ledger that does
    not exist yet, or one whose migrations have not been applied, must still
    print the documented shape (with empty counts) rather than nothing at
    all -- an empty preamble would leave the model with no timezone and no
    workday, which is worse than a preamble that says "nothing here yet".
    """
    # Even the config read is guarded: a hand-edited, unparseable
    # config.yaml must still leave the preamble with a workday and a
    # timezone rather than an empty block.
    try:
        tz, rollover_hour = _tz_and_rollover()
        today = timeutil.parse_day_expr("today", tz, rollover_hour)
    except Exception:
        tz, rollover_hour = config_mod.DEFAULTS["timezone"], config_mod.DEFAULTS["rollover_hour"]
        today = timeutil.parse_day_expr("today", tz, rollover_hour)
    ctx = {
        "today": today,
        "timezone": tz,
        "open_clients": [],
        "pending_contradictions": 0,
        "inbox_count": 0,
        "stale_connections": [],
    }
    try:
        conn = dbc.connect()
    except Exception:
        conn = None
    if conn is not None:
        try:
            ctx.update(_context_from_ledger(conn, tz=tz, rollover_hour=rollover_hour, today=today))
        except Exception:
            pass          # un-migrated / unreadable ledger: the shape above still stands
        finally:
            conn.close()

    if args.format == "json":
        print(json.dumps(ctx, indent=2))
    else:
        print(f"today: {ctx['today']}  timezone: {ctx['timezone']}")
        print(f"open clients: {', '.join(ctx['open_clients']) or '(none)'}")
        print(f"pending contradictions: {ctx['pending_contradictions']}  "
              f"inbox: {ctx['inbox_count']}")
        print(f"stale connections: {', '.join(ctx['stale_connections']) or '(none)'}")
    return 0


def _setup_timesheet(sp: argparse.ArgumentParser) -> None:
    # nargs='?' (not required): Ruling 1's contract test parses every
    # registered subcommand as `[name, "--format", "json"]` with no other
    # args, so a missing client must fail at runtime, not at parse time.
    sp.add_argument("client", nargs="?")
    sp.add_argument("--from", dest="from_day", default="today",
                    help="first workday (expression or YYYY-MM-DD); default today")
    sp.add_argument("--to", dest="to_day", default=None,
                    help="last workday; default = --from")
    sp.add_argument("--format", choices=("md", "csv", "text", "json"), default="md")
    sp.add_argument("--round", dest="round_to", type=int, default=None, metavar="MINUTES",
                    help="round each row's minutes UP to this increment (e.g. 15)")
    sp.add_argument("--internal", action="store_true",
                    help="include AI-usage fields (excluded by default: client-facing)")


@register("timesheet", setup=_setup_timesheet)
def _cmd_timesheet(args: argparse.Namespace) -> int:
    """`worklog timesheet <client> --from X --to Y` -- client-facing rows of
    (workday, ticket, summary, time), markdown by default. See
    query/timesheet.py for the shape and the --internal/--round contracts."""
    from work_ledger.query import timesheet as timesheet_mod

    if not args.client:
        print("worklog: timesheet requires a client name", file=sys.stderr)
        return 2
    tz, rollover_hour = _tz_and_rollover()
    from_day = timeutil.parse_day_expr(args.from_day, tz, rollover_hour)
    to_day = timeutil.parse_day_expr(args.to_day, tz, rollover_hour) \
        if args.to_day else from_day
    conn = _report_conn()
    try:
        try:
            report = timesheet_mod.timesheet_report(
                conn, args.client, from_day, to_day, tz=tz, rollover_hour=rollover_hour,
                round_to=args.round_to, internal=args.internal)
        except Exception as exc:
            print(f"worklog: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(timesheet_mod.render_timesheet(report, fmt=args.format))
        return 0
    finally:
        conn.close()


def _setup_contradictions(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("scope", nargs="?")
    sp.add_argument("--resolve", metavar="ID")
    sp.add_argument("--note")


@register("contradictions", setup=_setup_contradictions)
def _cmd_contradictions(args: argparse.Namespace) -> int:
    """`worklog contradictions [scope] [--resolve ID --note TEXT]`.

    With no `--resolve`, re-evaluates every scanned rule (DESIGN §8;
    `contradictions.evaluate` never touches `work_items`) and lists the
    currently-open contradictions in `scope` (or the whole ledger). With
    `--resolve`, marks that one contradiction resolved with a
    machine-written note and does nothing else -- the CLI never modifies
    Jira to resolve a contradiction (Spec §3.3, §20.24); this only ever
    updates the local `contradictions` row.
    """
    conn = _report_conn()
    try:
        if args.resolve:
            if not args.note:
                print("worklog: contradictions --resolve requires --note", file=sys.stderr)
                return 2
            contradictions_mod.resolve(conn, args.resolve, note=args.note)
            if args.format == "json":
                print(json.dumps({"resolved": args.resolve, "note": args.note}, indent=2))
            else:
                print(f"resolved {args.resolve}: {args.note}")
            return 0

        contradictions_mod.evaluate(conn)
        resolved = query_status.resolve_scope(conn, args.scope)
        filters = {"work_item_id": None, "client_id": None, "project_id": None}
        if resolved["kind"] == "work_item":
            filters["work_item_id"] = resolved["id"]
        elif resolved["kind"] == "project":
            filters["project_id"] = resolved["id"]
        elif resolved["kind"] == "client":
            filters["client_id"] = resolved["id"]
        items = query_scoped.open_contradictions(conn, filters)

        if args.format == "json":
            print(json.dumps({"kind": "contradictions", "scope": resolved, "items": items}, indent=2))
        else:
            print(f"Contradictions: {resolved.get('label', 'all')}")
            print("")
            if not items:
                print("(none open)")
            for item in items:
                short_id = item["id"][:8] if item.get("id") else "--------"
                print(f"  ⚠ {short_id} ({item['rule']}) [{item.get('work_item_id') or '-'}]: "
                      f"ledger says {item['ledger_claim']!r}, external says {item['external_claim']!r}")
            if items:
                print("")
                print("(use the full id from --format json with --resolve)")
        return 0
    finally:
        conn.close()


def _setup_evidence(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("entry", nargs="?")


@register("evidence", setup=_setup_evidence)
def _cmd_evidence(args: argparse.Namespace) -> int:
    if not args.entry:
        print("worklog: evidence requires an entry (activity id, remaining-item id, or ticket/LOCAL key)",
              file=sys.stderr)
        return 2
    conn = _report_conn()
    try:
        report = query_scoped.evidence_report(conn, args.entry)
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_inferences(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--limit", type=int, default=50)


@register("inferences", setup=_setup_inferences)
def _cmd_inferences(args: argparse.Namespace) -> int:
    conn = _report_conn()
    try:
        report = query_scoped.inbox_report(conn, limit=args.limit)
        _print_report(report, args.format)
        return 0
    finally:
        conn.close()


def _setup_sessions(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--limit", type=int, default=20)


@register("sessions", setup=_setup_sessions)
def _cmd_sessions(args: argparse.Namespace) -> int:
    """Recent capture sessions with their event counts -- the operator's view
    of what the hooks actually recorded."""
    conn = _report_conn()
    try:
        rows = capture_sessions.recent(conn, limit=args.limit)
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("(no sessions)")
        return 0
    for row in rows:
        state = row["end_reason"] or ("live" if not row["ended_at"] else "ended")
        problems = f"  problems={row['problem_count']}" if row["problem_count"] else ""
        print(f"{row['session_id']}  {row['started_at']} .. {row['last_seen_at']}  "
              f"[{state}]  events={row['event_count']}{problems}")
        if row["cwd"]:
            branch = f" ({row['git_branch']})" if row["git_branch"] else ""
            print(f"    {row['cwd']}{branch}")
    return 0


def _setup_stage_b(sp: argparse.ArgumentParser) -> None:
    # Not required, same reasoning as `_setup_reprocess`: the Ruling 1
    # contract test parses every subcommand with no other args.
    sp.add_argument("--session")
    sp.add_argument("--force", action="store_true")


@register("stage-b", setup=_setup_stage_b)
def _cmd_stage_b(args: argparse.Namespace) -> int:
    """Run the background LLM summarizer for one session (DESIGN §9.2).

    This is what `stage_b.spawn` launches from the Stop hook, and what a user
    runs by hand. It never fails loudly: Stage B not running is an ordinary
    outcome, so a rejected guard prints its reason and exits 0.

    The `WORK_LEDGER_NESTED` sentinel that `spawn` sets in this process's
    environment is consumed here, and only when `spawn` also set
    `WORK_LEDGER_STAGE_B_SPAWNED` -- this process *is* the Stage B runner, so
    treating its own sentinel as "someone else is already running Stage B"
    would leave the Stop-hook path permanently dead. Everything this process
    starts gets the sentinel back: `stage_b.invoke_claude` sets it explicitly
    on the `claude` child, which is the boundary that actually matters (DESIGN
    gap G8). A sentinel set by anyone else still refuses the run.
    """
    from work_ledger.infer import stage_b as stage_b_mod

    if os.environ.pop(stage_b_mod.SPAWNED_ENV, None):
        os.environ.pop(stage_b_mod.NESTED_ENV, None)

    if not args.session:
        print("worklog: stage-b requires --session (see `worklog sessions`)", file=sys.stderr)
        return 2

    conn = dbc.connect()
    try:
        result = stage_b_mod.run(conn, args.session, force=args.force)
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
        return 0
    if result["ran"]:
        actions = ", ".join(f"{count} {name.replace('_', ' ')}"
                            for name, count in sorted(result["remaining_actions"].items()))
        print(f"stage-b: {result['created']} created, {result['updated']} updated, "
              f"{result['remaining']} remaining item(s)"
              + (f" ({actions})" if actions else "")
              + f", {result['decisions']} decision(s), "
                f"{result['evidence']} evidence link(s)")
    else:
        print(f"stage-b: did not run [{result['status']}] ({result['reason']})")
    return 0


def _setup_reprocess(sp: argparse.ArgumentParser) -> None:
    # nargs='?' (not required), same reasoning as `_setup_day`: the Ruling 1
    # contract test parses every subcommand with no other args.
    sp.add_argument("session_id", nargs="?")
    sp.add_argument("--stage", choices=("a", "b", "both"), default="both")


@register("reprocess", setup=_setup_reprocess)
def _cmd_reprocess(args: argparse.Namespace) -> int:
    """Re-run reconciliation and interpretation for one session from the raw
    evidence (Spec §20.2). Safe to run repeatedly: it creates no duplicate
    events and no duplicate activities."""
    if not args.session_id:
        print("worklog: reprocess requires a session id (see `worklog sessions`)", file=sys.stderr)
        return 2
    conn = dbc.connect()
    try:
        result = capture_sessions.reprocess(conn, args.session_id, stage=args.stage)
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["found"] else 1
    if not result["found"]:
        print(f"worklog: no session {args.session_id}", file=sys.stderr)
        return 1
    print(f"session {result['session_id']}")
    print(f"  transcript events: {result['transcript_events']}")
    print(f"  git events: {result['git_events']}")
    for stage in ("stage_a", "stage_b"):
        if result[stage] is not None:
            print(f"  {stage}: {result[stage]}")
    return 0


def _setup_backup(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--label")
    sp.add_argument("--list", action="store_true")


@register("backup", setup=_setup_backup)
def _cmd_backup(args: argparse.Namespace) -> int:
    if args.list:
        rows = backup_mod.list_backups()
        if args.format == "json":
            print(json.dumps([{**row, "path": str(row["path"])} for row in rows], indent=2))
        else:
            if not rows:
                print("(no backups)")
            for row in rows:
                label = f" ({row['label']})" if row["label"] else ""
                print(f"{row['path']}{label}  {row['size_bytes']} bytes")
        return 0

    conn = dbc.connect()
    try:
        path = backup_mod.create_backup(conn, label=args.label)
    except ValueError as exc:
        print(f"worklog: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps({"path": str(path)}, indent=2))
    else:
        print(path)
    return 0


def _setup_restore(sp: argparse.ArgumentParser) -> None:
    # nargs='?' (not required), same reasoning as `_setup_day`: the Ruling 1
    # contract test parses every subcommand with no other args.
    sp.add_argument("path", nargs="?")
    sp.add_argument("--confirm", action="store_true")


@register("restore", setup=_setup_restore)
def _cmd_restore(args: argparse.Namespace) -> int:
    if not args.path:
        print("worklog: restore requires a backup path", file=sys.stderr)
        return 2
    if not args.confirm:
        print("worklog: restore requires --confirm (this replaces the live database)", file=sys.stderr)
        return 2
    backup_mod.restore(args.path, confirm=True)
    if args.format == "json":
        print(json.dumps({"restored_from": args.path}, indent=2))
    else:
        print(f"restored from {args.path}")
    return 0


def _setup_export(sp: argparse.ArgumentParser) -> None:
    # export gets its own parser (see build_parser: no parents=[common]), so
    # this --format is independent of the shared text/json report-render
    # flag -- it selects the output file format instead. "json" is still a
    # valid choice so the Ruling 1 contract test (`--format json` on every
    # subcommand) still passes.
    sp.add_argument("--format", choices=("json", "markdown", "csv"), default="json")
    sp.add_argument("--out")


@register("export", setup=_setup_export)
def _cmd_export(args: argparse.Namespace) -> int:
    conn = dbc.connect()
    try:
        if args.format == "json":
            out = pathlib.Path(args.out) if args.out else paths.export_dir() / "export.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(out.parent, 0o700)
            backup_mod.export_json(conn, out)
        elif args.format == "markdown":
            out = pathlib.Path(args.out) if args.out else paths.export_dir() / "history.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(out.parent, 0o700)
            tz, rollover_hour = _tz_and_rollover()
            backup_mod.export_markdown(conn, out, tz=tz, rollover_hour=rollover_hour)
        else:
            out = pathlib.Path(args.out) if args.out else paths.export_dir() / "csv"
            backup_mod.export_csv(conn, out)
        print(out)
        return 0
    finally:
        conn.close()


def _setup_import(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("path", nargs="?")
    sp.add_argument("--dry-run", action="store_true")


@register("import", setup=_setup_import)
def _cmd_import(args: argparse.Namespace) -> int:
    if not args.path:
        print("worklog: import requires a path", file=sys.stderr)
        return 2
    conn = dbc.connect()
    try:
        stats = backup_mod.import_json(conn, args.path, dry_run=args.dry_run)
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps(stats, indent=2))
    else:
        print(f"inserted={stats['inserted']} updated={stats['updated']} skipped={stats['skipped']}")
        if stats["conflicts"]:
            print(f"conflicts: {len(stats['conflicts'])}")
    return 0


def _setup_connections(sp: argparse.ArgumentParser) -> None:
    # nargs='?' on both positionals (not required), same reasoning as
    # `_setup_day`/`_setup_restore`: the Ruling 1 contract test parses every
    # subcommand with no other args.
    sp.add_argument("action", nargs="?", default="list",
                    choices=("list", "remove", "test", "repair"))
    sp.add_argument("name", nargs="?")
    sp.add_argument("--delete-credential", action="store_true")
    sp.add_argument("--confirm", action="store_true")
    sp.add_argument("--credential-reference")


def _print_connection_test_result(result: dict) -> None:
    print(f"{result['name']}: {result['state']}")
    for check in result["checks"]:
        detail = f"  ({check['detail']})" if check["detail"] else ""
        print(f"  {check['name']}: {check['state']}{detail}")


@register("connections", setup=_setup_connections)
def _cmd_connections(args: argparse.Namespace) -> int:
    conn = dbc.connect()
    try:
        if args.action == "list":
            rows = connections_mod.list_connections(conn, include_disabled=True)
            if args.format == "json":
                print(json.dumps([dict(r) for r in rows], indent=2))
            else:
                if not rows:
                    print("(no connections)")
                for r in rows:
                    state = "enabled" if r["enabled"] else "disabled"
                    print(f"{r['name']}  adapter={r['adapter']}  health={r['health_state']}  ({state})")
            return 0

        if args.action == "test":
            if not args.name:
                print("worklog: connections test requires a name", file=sys.stderr)
                return 2
            try:
                result = connections_mod.test_connection(conn, args.name)
            except ValueError as exc:
                print(f"worklog: {exc}", file=sys.stderr)
                return 2
            if args.format == "json":
                print(json.dumps(result, indent=2))
            else:
                _print_connection_test_result(result)
            return 0

        if args.action == "repair":
            if not args.name:
                print("worklog: connections repair requires a name", file=sys.stderr)
                return 2
            try:
                result = connections_mod.repair(
                    conn, args.name, credential_reference=args.credential_reference,
                )
            except ValueError as exc:
                print(f"worklog: {exc}", file=sys.stderr)
                return 2
            if args.format == "json":
                print(json.dumps(result, indent=2))
            else:
                _print_connection_test_result(result)
            return 0

        # action == "remove"
        if not args.name:
            print("worklog: connections remove requires a name", file=sys.stderr)
            return 2
        if args.delete_credential and not args.confirm:
            print("worklog: --delete-credential requires --confirm", file=sys.stderr)
            return 2
        try:
            result = connections_mod.remove(
                conn, args.name, delete_credential=args.delete_credential, confirmed=args.confirm,
            )
        except ValueError as exc:
            print(f"worklog: {exc}", file=sys.stderr)
            return 2

        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            suffix = " (credential deleted)" if result["credential_deleted"] else ""
            print(f"removed {args.name}{suffix}")
        return 0
    finally:
        conn.close()


def _setup_jira(sp: argparse.ArgumentParser) -> None:
    # nargs='?' with a default (Ruling 1's contract test parses every
    # subcommand as `[name, "--format", "json"]` and nothing else).
    sp.add_argument("action", nargs="?", default="issues",
                    choices=("discover", "plan-refresh", "ingest", "issues", "refresh"))
    sp.add_argument("--connection")
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--stale", action="store_true")


@register("jira", setup=_setup_jira)
def _cmd_jira(args: argparse.Namespace) -> int:
    """`worklog jira discover|plan-refresh|ingest|issues`.

    The model-mediated Jira loop (DESIGN §4.5, §10.3, gap G2). Nothing here
    calls an MCP tool -- a CLI process has no channel to one. `plan-refresh`
    tells the model what to fetch and with which `mcp__<server>__*` tool;
    the model pipes the tool results back into `ingest` on stdin, which
    treats them as untrusted input.
    """
    if args.action == "discover":
        servers = jira_mcp.discover_servers()
        candidates = jira_mcp.jira_candidates(servers)
        if args.format == "json":
            print(json.dumps({"servers": servers,
                              "jira_candidates": [s["name"] for s in candidates]}, indent=2))
        else:
            if not servers:
                print("(no MCP servers configured)")
            candidate_names = {s["name"] for s in candidates}
            for server in servers:
                mark = "jira?" if server["name"] in candidate_names else "     "
                print(f"  {mark} {server['name']}  (from {server['source']})")
        return 0

    conn = dbc.connect()
    try:
        if args.action == "plan-refresh":
            limit = (args.limit if args.limit is not None
                     else jira_mcp.default_refresh_limit())
            plan = jira_mcp.plan_refresh(conn, connection_name=args.connection, limit=limit)
            if args.format == "json":
                print(json.dumps(plan, indent=2))
            else:
                if not plan["connections"]:
                    print("(nothing stale to refresh)")
                for entry in plan["connections"]:
                    print(f"{entry['connection']}: call {entry['tool']} for "
                          f"{', '.join(entry['issue_keys'])}")
            return 0

        if args.action == "refresh":
            # Same policy as the hook-time path (jira.refresh, Ruling 8,
            # allowlist item (d)): only `adapter='jira-rest'` connections are
            # ever contacted here -- an `adapter='mcp'` connection has no
            # channel a CLI process can call (DESIGN gap G2) and must go
            # through `worklog jira plan-refresh`/`ingest` instead.
            limit = (args.limit if args.limit is not None
                     else jira_mcp.default_refresh_limit())
            if args.connection:
                # include_disabled=True so a disabled connection is reported
                # distinctly from one that does not exist at all (code review
                # MINOR), rather than both collapsing into "no connection
                # named ...".
                row = next(
                    (c for c in connections_mod.list_connections(conn, include_disabled=True)
                     if c["name"] == args.connection),
                    None,
                )
                if row is None:
                    print(f"worklog: no connection named {args.connection!r}", file=sys.stderr)
                    return 2
                if not row["enabled"]:
                    print(f"worklog: {args.connection!r} is disabled", file=sys.stderr)
                    return 2
                if row["adapter"] != "jira-rest":
                    print(f"worklog: {args.connection!r} is an MCP connection -- refresh it with "
                          "`worklog jira plan-refresh` / `worklog jira ingest`", file=sys.stderr)
                    return 2
                result = jira_refresh.refresh_rest(
                    conn, row, limit=limit, now=timeutil.utc_now_iso(),
                )
                result = {**result, "connections": {row["name"]: result["health"]}, "skipped_mcp": []}
            else:
                result = jira_refresh.refresh_all_rest(conn, limit=limit)
            if args.format == "json":
                print(json.dumps(result, indent=2))
            else:
                print(f"updated {result['updated']}, failed {result['failed']}")
                for name, state in result["connections"].items():
                    print(f"  {name}: {state}")
                if result["skipped_mcp"]:
                    print(f"skipped (MCP, use plan-refresh/ingest): {', '.join(result['skipped_mcp'])}")
            return 0

        if args.action == "ingest":
            if not args.connection:
                print("worklog: jira ingest requires --connection NAME", file=sys.stderr)
                return 2
            raw = sys.stdin.read()
            try:
                payload = json.loads(raw) if raw.strip() else None
            except ValueError as exc:
                print(f"worklog: jira ingest expects JSON on stdin ({exc})", file=sys.stderr)
                return 2
            if payload is None:
                print("worklog: jira ingest expects JSON on stdin", file=sys.stderr)
                return 2
            try:
                result = jira_mcp.ingest(conn, args.connection, payload)
            except ValueError as exc:
                print(f"worklog: {exc}", file=sys.stderr)
                return 2
            if args.format == "json":
                print(json.dumps(result, indent=2))
            else:
                print(f"updated {result['updated']}, rejected {len(result['rejected'])}, "
                      f"unknown {len(result['unknown_keys'])}, "
                      f"contradictions opened {result['contradictions_opened']}")
            return 0

        # action == "issues"
        rows = jira_mcp.jira_work_items(conn, stale_only=args.stale)
        if args.format == "json":
            print(json.dumps({"kind": "jira-issues", "items": rows}, indent=2))
        else:
            if not rows:
                print("(no Jira-backed work items)")
            for row in rows:
                flag = " [stale]" if row["stale"] else ""
                print(f"  {row['external_key']}  {row['status'] or '-'}  {row['title']}{flag}")
        return 0
    finally:
        conn.close()


def _setup_setup(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--reset", action="store_true")
    sp.add_argument("--only", choices=setupwizard.STEPS)
    sp.add_argument("--non-interactive", action="store_true")
    sp.add_argument("--answers", help="path to a JSON file of non-interactive answers")


@register("setup", setup=_setup_setup)
def _cmd_setup(args: argparse.Namespace) -> int:
    """`worklog setup [--reset] [--only STEP] [--non-interactive --answers FILE]`.

    `--answers FILE` implies non-interactive (the file supplies every answer);
    `--non-interactive` on its own runs with defaults and prompts for nothing.
    Exit status is 0 unless an essential step failed -- a failed Jira
    connection degrades the run without failing it (Spec §13).
    """
    answers = None
    if args.answers:
        try:
            answers = json.loads(pathlib.Path(args.answers).read_text())
        except (OSError, ValueError) as exc:
            print(f"worklog: could not read --answers file: {exc}", file=sys.stderr)
            return 2
        if not isinstance(answers, dict):
            print("worklog: --answers file must contain a JSON object", file=sys.stderr)
            return 2
    elif args.non_interactive:
        answers = {}

    def prompt(question: str) -> str:
        # Questions go to stderr so `--format json` leaves stdout as exactly
        # the JSON payload (same convention as `uninstall --data`).
        print(question, end="", file=sys.stderr, flush=True)
        return sys.stdin.readline().rstrip("\n")

    conn = dbc.connect()
    try:
        migrations.ensure_current(conn)
        report = setupwizard.run(
            conn, answers=answers, reset=args.reset, only=args.only, prompt=prompt,
        )
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        for step in report["steps"]:
            print(f"  [{step['state']:>4}] {step['name']}: {step['detail']}")
        print()
        if report.get("text"):
            print(report["text"], end="")
        else:
            print(f"setup {report['overall']}")
    return 1 if report["overall"] == "failed" else 0


@register("doctor")
def _cmd_doctor(args: argparse.Namespace) -> int:
    # G1: doctor is a read path too, and the sweep is exactly the kind of
    # deferred bookkeeping a health check should settle before reporting.
    try:
        conn = dbc.connect()
        try:
            _sweep_stale_sessions(conn)
        finally:
            conn.close()
    except Exception:
        pass
    report = doctor_mod.run()
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(f"overall: {report['overall']}")
        for c in report["checks"]:
            print(f"  [{c['state']:>4}] {c['name']}: {c['detail']}")
    return 0 if report["overall"] != "unhealthy" else 1


def _print_inventory_text(inv: dict, *, stream=None) -> None:
    stream = stream if stream is not None else sys.stdout
    db = inv["database"]
    print(f"home: {inv['home']}", file=stream)
    print(f"database: {db['path']} ({'exists' if db['exists'] else 'missing'}, {db['bytes']} bytes)", file=stream)
    for name, count in inv["counts"].items():
        print(f"  {name}: {count}", file=stream)
    print(f"backups: {inv['backups']}  exports: {inv['exports']}  logs: {inv['logs']}", file=stream)
    if inv["credential_references"]:
        print("credential references:", file=stream)
        for ref in inv["credential_references"]:
            print(f"  {ref}", file=stream)


def _setup_uninstall(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--data", action="store_true")
    sp.add_argument("--delete-credentials", action="store_true")


@register("uninstall", setup=_setup_uninstall)
def _cmd_uninstall(args: argparse.Namespace) -> int:
    if not args.data:
        result = uninstall_mod.run()  # data=False: inventory only, deletes nothing
        if args.format == "json":
            print(json.dumps(result, indent=2))
        else:
            _print_inventory_text(result["inventory"])
        return 0

    # In JSON mode, stdout must be exactly the JSON payload: the human-
    # readable inventory and the confirmation prompt go to stderr instead.
    human_stream = sys.stderr if args.format == "json" else sys.stdout
    inv = uninstall_mod.inventory()
    _print_inventory_text(inv, stream=human_stream)
    print(f'Type "{uninstall_mod.CONFIRMATION_PHRASE}" to continue: ', end="", file=human_stream, flush=True)
    typed = input()
    result = uninstall_mod.run(
        data=True, delete_credentials=args.delete_credentials, confirmation=typed,
    )

    if args.format == "json":
        print(json.dumps(result, indent=2))
        return 1 if result["refused"] else 0

    if result["refused"]:
        print(f"worklog: {result['refused']}", file=sys.stderr)
        return 1

    print(f"deleted: {result['inventory']['home']}")
    removed = [c["reference"] for c in result["deleted"]["credentials"] if c["removed"]]
    failed = [c for c in result["deleted"]["credentials"] if not c["removed"]]
    if removed:
        print("removed credentials: " + ", ".join(removed))
    for c in failed:
        print(f"worklog: failed to remove credential {c['reference']}: {c['detail']}", file=sys.stderr)
    return 0


def _setup_githooks(sp: argparse.ArgumentParser) -> None:
    # nargs='?' default 'status': Ruling 1's contract test parses every
    # subcommand as `[name, "--format", "json"]` with no other args.
    sp.add_argument("action", nargs="?", default="status",
                    choices=("install", "remove", "capture", "status"))
    sp.add_argument("--repo", help="repository path (default: current directory)")
    sp.add_argument("--all-mapped", action="store_true",
                    help="install/remove for every mapped repository")


@register("githooks", setup=_setup_githooks)
def _cmd_githooks(args: argparse.Namespace) -> int:
    """`worklog githooks install|remove|capture|status` -- the opt-in git
    post-commit capture (see githooks.py).

    `capture` is what the hook itself runs: it must NEVER exit non-zero or
    print to stdout/stderr noise that could confuse a porcelain -- a broken
    ledger must never break `git commit`.
    """
    from work_ledger import githooks as githooks_mod

    if args.action == "capture":
        try:
            conn = dbc.connect(hook_mode=True)
            try:
                githooks_mod.capture(conn, args.repo or os.getcwd(), hook_mode=True)
            finally:
                conn.close()
        except BaseException:
            try:
                from work_ledger import paths as _paths
                import traceback
                with open(_paths.log_dir() / "githooks.log", "a") as f:
                    f.write(f"{timeutil.utc_now_iso()} capture failed\n")
                    traceback.print_exc(file=f)
            except BaseException:
                pass
        return 0

    conn = dbc.connect()
    try:
        migrations.ensure_current(conn)
        if args.action == "status":
            repos = githooks_mod.installed_repos(conn)
            if args.format == "json":
                print(json.dumps({"repos": repos}, indent=2))
            else:
                if not repos:
                    print("(no git hooks installed)")
                for repo in repos:
                    print(repo)
            return 0

        targets = []
        if args.all_mapped:
            rows = conn.execute(
                "SELECT root_path FROM repositories WHERE deleted_at IS NULL").fetchall()
            targets = [r["root_path"] for r in rows]
        elif args.repo:
            targets = [args.repo]
        else:
            targets = [os.getcwd()]

        results = []
        for repo in targets:
            try:
                if args.action == "install":
                    results.append(githooks_mod.install_hook(conn, repo))
                else:
                    results.append(githooks_mod.remove_hook(conn, repo))
            except ValueError as exc:
                print(f"worklog: {exc}", file=sys.stderr)
                return 2
        if args.format == "json":
            print(json.dumps({"action": args.action, "results": results}, indent=2))
        else:
            for result in results:
                verb = "installed" if args.action == "install" else "removed"
                print(f"{verb}: {result['repo']}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":          # pragma: no cover -- exercised via subprocess
    raise SystemExit(main())


def _setup_week(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("scope", nargs="?")
    sp.add_argument("--ending", default="today",
                    help="last workday of the week (default today); week starts Monday")
    sp.add_argument("--format", choices=("text", "md", "json"), default="text")
    sp.add_argument("--internal", action="store_true",
                    help="include AI-usage and cost fields")


@register("week", setup=_setup_week)
def _cmd_week(args: argparse.Namespace) -> int:
    """`worklog week [client]` -- Monday-to-date digest per client/ticket."""
    from work_ledger.query import week as week_mod

    tz, rollover_hour = _tz_and_rollover()
    ending = timeutil.parse_day_expr(args.ending, tz, rollover_hour)
    conn = _report_conn()
    try:
        try:
            report = week_mod.week_report(
                conn, args.scope, ending=ending, tz=tz, rollover_hour=rollover_hour,
                internal=args.internal)
        except Exception as exc:
            print(f"worklog: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(week_mod.render_week(report, fmt=args.format))
        return 0
    finally:
        conn.close()


def _setup_standup(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("scope", nargs="?")


@register("standup", setup=_setup_standup)
def _cmd_standup(args: argparse.Namespace) -> int:
    """`worklog standup [client]` -- Yesterday / Today / Blockers."""
    from work_ledger.query import week as week_mod

    tz, rollover_hour = _tz_and_rollover()
    conn = _report_conn()
    try:
        try:
            report = week_mod.standup_report(
                conn, args.scope, tz=tz, rollover_hour=rollover_hour)
        except Exception as exc:
            print(f"worklog: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps(report, indent=2))
        else:
            print(week_mod.render_standup(report))
        return 0
    finally:
        conn.close()
