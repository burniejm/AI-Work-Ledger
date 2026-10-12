import argparse, io, contextlib, json, os, tempfile, unittest
from work_ledger import cli, __version__
from work_ledger import config as config_mod
from work_ledger import connections as conns
from work_ledger import entities as e, work_items as wi, ids, timeutil
from work_ledger.db import connection as dbc, migrations as m

class TestCli(unittest.TestCase):
    def test_version(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["version"])
        self.assertEqual(rc, 0)
        self.assertIn(__version__, buf.getvalue())

    def test_unknown_command_returns_2_but_never_raises(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["definitely-not-a-command"]), 2)

    def test_no_command_returns_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 2)

    def test_format_flag_is_accepted_after_every_subcommand(self):
        # Ruling 1: --format lives on each subparser via parents=[common_parser()],
        # so it is legal AFTER the subcommand name (argparse never routes it to the
        # top-level parser). Parsing must succeed for every registered subcommand.
        parser = cli.build_parser()
        for name in cli.registered_names():
            args = parser.parse_args([name, "--format", "json"])
            self.assertEqual(args.command, name)
            self.assertEqual(args.format, "json")

    def test_format_defaults_to_text(self):
        args = cli.build_parser().parse_args(["version"])
        self.assertEqual(args.format, "text")


class TestCliCorrect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

        conn = dbc.connect()
        m.apply_all(conn)
        self.cl = e.upsert_client(conn, "Acme")
        other_cl = e.upsert_client(conn, "Globex")
        self.pr = e.upsert_project(conn, self.cl, "Replay")
        e.upsert_project(conn, other_cl, "Replay")  # same-named project, different client
        a = wi.upsert_external(conn, "conn1", "AB-326", title="Replay hardware")
        now = timeutil.utc_now_iso()
        self.act = ids.new_id()
        conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,project_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.act, a, self.cl, None, "test", "Tested upload recovery", now,
             "medium", "stage_a", "idem-1", "h", "dev1", now, now),
        )
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_move_activity_project_only_scopes_to_activity_client(self):
        # Important 3: --project without --client must resolve within the
        # activity's *current* client, not `client_id IS NULL` (which would
        # never match a real project and always raise).
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["correct", "move-activity", "--activity", self.act,
                            "--project", "Replay", "--reason", "x"])
        self.assertEqual(rc, 0, buf.getvalue())

        conn = dbc.connect()
        row = conn.execute("SELECT project_id FROM activities WHERE id=?", (self.act,)).fetchone()
        conn.close()
        self.assertEqual(row["project_id"], self.pr)


class TestCliStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

        conn = dbc.connect()
        m.apply_all(conn)
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_status_subcommand_honors_configured_status_workdays(self):
        # `_cmd_status` must read `reports.status_workdays` from config
        # (mirroring the `stale` subcommand's `reports.stale_after_days`
        # pattern) rather than hardcoding query.status.status_report's
        # own default of 7.
        config_mod.save({"reports": {"status_workdays": 3}})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["status", "--format", "json"])
        self.assertEqual(rc, 0, buf.getvalue())
        rep = json.loads(buf.getvalue())
        self.assertEqual(rep["window_workdays"], 3)


class TestCliContradictions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

        conn = dbc.connect()
        m.apply_all(conn)
        cl = e.upsert_client(conn, "Acme")
        self.item = wi.upsert_external(conn, "conn1", "AB-76", title="Upload recovery", client_id=cl)
        conn.execute("INSERT INTO remaining_items(id,work_item_id,title,normalized_key,state,"
                     "first_seen_at,last_seen_at,confidence,source,origin_device_id,created_at,updated_at)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ids.new_id(), self.item, "Retest on hardware", "hardware-retest", "open",
                      "t", "t", "high", "stage_a", "d", "t", "t"))
        conn.execute("UPDATE work_items SET jira_status='Done', jira_status_category='done',"
                     " jira_resolution='Fixed', jira_stale=0 WHERE id=?", (self.item,))
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_list_via_json_then_resolve_by_id_end_to_end(self):
        # IMPORTANT 1 regression: the CLI's JSON output must carry a
        # per-contradiction `id` -- otherwise `--resolve ID` is unusable
        # through the tool (there is no other way to learn a valid id).
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["contradictions", "--format", "json"])
        self.assertEqual(rc, 0, buf.getvalue())
        rep = json.loads(buf.getvalue())
        self.assertEqual(len(rep["items"]), 1)
        contradiction_id = rep["items"][0]["id"]
        self.assertTrue(contradiction_id)
        self.assertEqual(rep["items"][0]["rule"], "jira_done_open_remaining")

        # The text rendering must show a short id form, not the raw dict.
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc = cli.main(["contradictions"])
        self.assertEqual(rc, 0, buf2.getvalue())
        self.assertIn(contradiction_id[:8], buf2.getvalue())

        buf3 = io.StringIO()
        with contextlib.redirect_stdout(buf3):
            rc = cli.main(["contradictions", "--resolve", contradiction_id, "--note", "fixed manually"])
        self.assertEqual(rc, 0, buf3.getvalue())

        conn = dbc.connect()
        row = conn.execute("SELECT status, resolution_note FROM contradictions WHERE id=?",
                            (contradiction_id,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "resolved")
        self.assertEqual(row["resolution_note"], "fixed manually")

    def test_resolve_unknown_id_reports_failure_not_silent_success(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.main(["contradictions", "--resolve", "does-not-exist", "--note", "n/a"])
        self.assertEqual(rc, 1)
        self.assertIn("does-not-exist", buf.getvalue())


class TestCliContext(unittest.TestCase):
    """`worklog context --format json` -- the cheap preamble the worklog
    skill runs inline (Task 37). One shot, no Jira calls, never a traceback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def _context(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["context", "--format", "json"])
        self.assertEqual(rc, 0, buf.getvalue())
        return json.loads(buf.getvalue())

    def test_context_on_an_uninitialized_ledger_still_returns_the_full_shape(self):
        # The skill runs this inline with `|| true`; a missing/un-migrated
        # database must still produce the documented keys rather than a
        # traceback or an empty stdout.
        ctx = self._context()
        self.assertEqual(
            sorted(ctx),
            ["inbox_count", "open_clients", "pending_contradictions", "stale_connections",
             "timezone", "today"],
        )
        self.assertEqual(ctx["open_clients"], [])
        self.assertEqual(ctx["pending_contradictions"], 0)
        self.assertEqual(ctx["inbox_count"], 0)
        self.assertEqual(ctx["stale_connections"], [])

    def test_context_reports_today_and_the_configured_timezone(self):
        ctx = self._context()
        tz, rollover = cli._tz_and_rollover()
        self.assertEqual(ctx["timezone"], tz)
        self.assertEqual(ctx["today"], timeutil.parse_day_expr("today", tz, rollover))

    def test_context_summarizes_open_clients_contradictions_inbox_and_stale_connections(self):
        conn = dbc.connect()
        m.apply_all(conn)
        cl = e.upsert_client(conn, "Acme")
        done_client = e.upsert_client(conn, "Finished Co")
        open_item = wi.upsert_external(conn, "conn1", "AB-76", title="Upload recovery", client_id=cl)
        closed_item = wi.upsert_external(conn, "conn1", "AB-1", title="Shipped", client_id=done_client)
        conn.execute("UPDATE work_items SET local_state='completed' WHERE id=?", (closed_item,))
        now = timeutil.utc_now_iso()
        conn.execute(
            "INSERT INTO contradictions(id,rule,work_item_id,ledger_claim,external_claim,status,"
            "detected_at,origin_device_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), "jira_done_open_remaining", open_item, "open", "Done", "open",
             now, "dev1", now, now),
        )
        conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,kind,summary,started_at,confidence,"
            "source,idempotency_key,source_hash,origin_device_id,created_at,updated_at,in_inbox)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ids.new_id(), open_item, cl, "test", "unattributed work", now, "low",
             "stage_a", "idem-ctx-1", "h", "dev1", now, now, 1),
        )
        # Three connections, all healthy but for one, so "stale" is proved to
        # mean *stale data or unhealthy connection* -- not merely "exists".
        stale_conn = conns.upsert_connection(conn, name="acme-jira", adapter="mcp",
                                             mcp_server_name="atlassian", cloud_id="cid")
        fresh_conn = conns.upsert_connection(conn, name="globex-jira", adapter="mcp",
                                             mcp_server_name="atlassian", cloud_id="cid")
        conns.upsert_connection(conn, name="expired-jira", adapter="mcp",
                                mcp_server_name="atlassian", cloud_id="cid")
        conn.execute("UPDATE connections SET health_state='healthy' WHERE name<>'expired-jira'")
        conn.execute("UPDATE connections SET health_state='expired' WHERE name='expired-jira'")
        conn.execute("UPDATE work_items SET connection_id=?, jira_stale=1 WHERE id=?",
                     (stale_conn, open_item))
        conn.execute("UPDATE work_items SET connection_id=?, jira_stale=0, jira_fetched_at=? "
                     "WHERE id=?", (fresh_conn, now, closed_item))
        conn.close()

        ctx = self._context()
        self.assertEqual(ctx["open_clients"], ["Acme"])
        self.assertEqual(ctx["pending_contradictions"], 1)
        self.assertEqual(ctx["inbox_count"], 1)
        self.assertEqual(ctx["stale_connections"], ["acme-jira", "expired-jira"])

    def test_context_text_format_is_human_readable(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["context"])
        self.assertEqual(rc, 0, buf.getvalue())
        out = buf.getvalue()
        self.assertIn("today:", out)
        self.assertIn("timezone:", out)

    def test_context_survives_an_unparseable_config(self):
        # The preamble runs as `... 2>/dev/null || true`, so a hand-broken
        # config must still yield a workday and a timezone rather than an
        # empty block the model cannot orient with.
        from work_ledger import paths
        paths.ensure_dirs()
        paths.config_path().write_text("timezone: [this is not a scalar\n")
        ctx = self._context()
        self.assertEqual(ctx["timezone"], config_mod.DEFAULTS["timezone"])
        self.assertRegex(ctx["today"], r"^\d{4}-\d{2}-\d{2}$")


class TestCliBackup(unittest.TestCase):
    """Minor #6 (final review): `worklog backup --label` must surface a
    rejected label's `ValueError` rather than letting a traceback through."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        conn = dbc.connect()
        m.apply_all(conn)
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_a_label_with_a_slash_is_refused_with_a_clear_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["backup", "--label", "pre/post migration"])
        self.assertEqual(rc, 2)
        self.assertIn("label", err.getvalue())

    def test_an_ordinary_label_succeeds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["backup", "--label", "pre-restore"])
        self.assertEqual(rc, 0, out.getvalue())


class TestCliRepos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

        conn = dbc.connect()
        m.apply_all(conn)
        self.unmapped = e.upsert_repository(conn, "/Users/x/scout")
        self.mapped = e.upsert_repository(conn, "/Users/x/replay", name="replay")
        cl = e.upsert_client(conn, "Acme")
        pr = e.upsert_project(conn, cl, "Replay")
        e.map_repository(conn, self.mapped, client_id=cl, project_id=pr,
                         confidence="high", source="setup")
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_list_shows_mapped_and_unmapped_repositories(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["repos", "--format", "json"])
        self.assertEqual(rc, 0, buf.getvalue())
        items = {r["root_path"]: r for r in json.loads(buf.getvalue())}
        self.assertEqual(items["/Users/x/replay"]["client"], "Acme")
        self.assertEqual(items["/Users/x/replay"]["project"], "Replay")
        self.assertIsNone(items["/Users/x/scout"]["client"])

    def test_map_creates_client_project_and_mapping(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["repos", "map", "--repo", "/Users/x/scout",
                          "--client", "Globex", "--project", "Portal", "--format", "json"])
        self.assertEqual(rc, 0, buf.getvalue())

        conn = dbc.connect()
        row = conn.execute(
            "SELECT rm.confidence, rm.source, c.name AS client, p.name AS project "
            "FROM repository_mappings rm "
            "JOIN clients c ON c.id = rm.client_id JOIN projects p ON p.id = rm.project_id "
            "WHERE rm.repository_id=?", (self.unmapped,),
        ).fetchone()
        conn.close()
        self.assertEqual(row["client"], "Globex")
        self.assertEqual(row["project"], "Portal")
        self.assertEqual(row["confidence"], "high")
        self.assertEqual(row["source"], "manual")

    def test_map_can_resolve_a_repo_by_name(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["repos", "map", "--repo", "replay",
                          "--client", "Acme", "--format", "json"])
        self.assertEqual(rc, 0, buf.getvalue())

    def test_map_requires_repo_and_client(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["repos", "map", "--client", "Acme"])
        self.assertEqual(rc, 2)
        self.assertIn("--repo", err.getvalue())

    def test_map_unknown_repo_is_reported_not_raised(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["repos", "map", "--repo", "/nope", "--client", "Acme"])
        self.assertEqual(rc, 2)
        self.assertIn("/nope", err.getvalue())


class TestCliConnectionsRepair(unittest.TestCase):
    """`worklog connections repair <name> --credential-reference …` --
    the documented recovery path (spec §12); Important #1 requires this
    CLI path to refuse a pasted raw secret rather than persist it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

        conn = dbc.connect()
        m.apply_all(conn)
        conns.upsert_connection(conn, name="acme-jira", adapter="jira-rest",
                                credential_reference="keychain://work-ledger/acme-jira")
        conn.close()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()

    def test_repair_with_a_raw_secret_is_refused_and_never_persisted_or_echoed(self):
        secret = "sk-super-secret-raw-token-do-not-store"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["connections", "repair", "acme-jira",
                          "--credential-reference", secret])
        self.assertEqual(rc, 2)
        self.assertNotIn(secret, out.getvalue())
        self.assertNotIn(secret, err.getvalue())

        conn = dbc.connect()
        row = conn.execute(
            "SELECT credential_reference FROM connections WHERE name='acme-jira'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["credential_reference"], "keychain://work-ledger/acme-jira")

    def test_repair_with_a_real_reference_still_succeeds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["connections", "repair", "acme-jira",
                          "--credential-reference", "env://ACME_JIRA_TOKEN",
                          "--format", "json"])
        self.assertEqual(rc, 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertEqual(result["name"], "acme-jira")

        conn = dbc.connect()
        row = conn.execute(
            "SELECT credential_reference FROM connections WHERE name='acme-jira'"
        ).fetchone()
        conn.close()
        self.assertEqual(row["credential_reference"], "env://ACME_JIRA_TOKEN")
