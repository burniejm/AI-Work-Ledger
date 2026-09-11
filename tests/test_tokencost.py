import json, os, tempfile, unittest
from pathlib import Path

from work_ledger import ids, timeutil, transcript, work_items as wi
from work_ledger.capture import sessions as S
from work_ledger.db import connection as dbc, migrations as m
from work_ledger.query import tokencost


def _record(uuid, model=None, usage=None, text="hi", role="assistant"):
    message = {"role": role, "content": [{"type": "text", "text": text}]}
    if model:
        message["model"] = model
    if usage:
        message["usage"] = usage
    return {"type": role if role in ("user", "assistant") else "assistant",
            "uuid": uuid, "parentUuid": None, "sessionId": "s1",
            "timestamp": "2026-09-01T12:00:00.000Z", "message": message}


class TestSummarizeUsage(unittest.TestCase):
    def test_assistant_records_surface_model_and_usage(self):
        rec = _record("u1", model="claude-opus-5",
                      usage={"input_tokens": 100, "output_tokens": 20,
                             "cache_read_input_tokens": 500,
                             "cache_creation_input_tokens": 30})
        summary = transcript.summarize_record(rec)
        self.assertEqual(summary["model"], "claude-opus-5")
        self.assertEqual(summary["usage"]["input_tokens"], 100)
        self.assertEqual(summary["usage"]["cache_read_input_tokens"], 500)

    def test_records_without_usage_are_none_safe(self):
        summary = transcript.summarize_record(_record("u2", role="user"))
        self.assertIsNone(summary["model"])
        self.assertIsNone(summary["usage"])


class _TokenCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = os.path.join(self.tmp.name, "wl")
        self.conn = dbc.connect()
        m.apply_all(self.conn)

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]
        self.tmp.cleanup()

    def _transcript(self, records):
        path = Path(self.tmp.name) / "t.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return str(path)

    def _ingest(self, records, session_id="s1"):
        path = self._transcript(records)
        S.upsert(self.conn, session_id=session_id, transcript_path=path,
                 occurred_at="2026-09-01T12:00:00.000Z")
        return S.reconcile_transcript(self.conn, session_id)


class TestAccumulation(_TokenCase):
    def test_reconcile_accumulates_per_model_and_is_idempotent(self):
        self._ingest([
            _record("u1", model="claude-opus-5",
                    usage={"input_tokens": 100, "output_tokens": 20}),
            _record("u2", model="claude-opus-5",
                    usage={"input_tokens": 50, "output_tokens": 10,
                           "cache_read_input_tokens": 400}),
            _record("u3", model="claude-haiku-4-5-20251001",
                    usage={"input_tokens": 7, "output_tokens": 3}),
            _record("u4", role="user"),
        ])
        rows = {r["model"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM session_tokens ORDER BY model")}
        self.assertEqual(rows["claude-opus-5"]["input_tokens"], 150)
        self.assertEqual(rows["claude-opus-5"]["output_tokens"], 30)
        self.assertEqual(rows["claude-opus-5"]["cache_read_tokens"], 400)
        self.assertEqual(rows["claude-haiku-4-5-20251001"]["input_tokens"], 7)
        # Replay: watermark held, nothing double counted.
        self.assertEqual(S.reconcile_transcript(self.conn, "s1"), 0)
        again = self.conn.execute(
            "SELECT input_tokens FROM session_tokens WHERE model='claude-opus-5'"
        ).fetchone()
        self.assertEqual(again["input_tokens"], 150)


class TestCost(unittest.TestCase):
    def test_known_model_costs_from_rate_table(self):
        cost = tokencost.cost_usd("claude-opus-5", {
            "input_tokens": 1_000_000, "output_tokens": 100_000,
            "cache_read_tokens": 0, "cache_creation_tokens": 0})
        self.assertAlmostEqual(cost, 5.0 + 2.5, places=6)

    def test_dated_model_id_matches_by_prefix(self):
        cost = tokencost.cost_usd("claude-haiku-4-5-20251001", {
            "input_tokens": 1_000_000, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0})
        self.assertAlmostEqual(cost, 1.0, places=6)

    def test_unknown_model_returns_none_never_guessed(self):
        self.assertIsNone(tokencost.cost_usd("some-other-model", {
            "input_tokens": 10, "output_tokens": 10,
            "cache_read_tokens": 0, "cache_creation_tokens": 0}))

    def test_cache_reads_are_cheaper_than_input(self):
        plain = tokencost.cost_usd("claude-sonnet-5", {
            "input_tokens": 1_000_000, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0})
        cached = tokencost.cost_usd("claude-sonnet-5", {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 1_000_000, "cache_creation_tokens": 0})
        self.assertLess(cached, plain)


class TestTicketRollup(_TokenCase):
    def test_tokens_for_work_item_sums_its_sessions(self):
        self._ingest([
            _record("u1", model="claude-opus-5",
                    usage={"input_tokens": 100, "output_tokens": 20}),
        ])
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        item = wi.upsert_external(self.conn, "c1", "AB-76", title="t")
        srow = self.conn.execute(
            "SELECT id FROM sessions WHERE session_id='s1'").fetchone()["id"]
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,session_row_id,kind,summary,started_at,"
            "confidence,source,idempotency_key,source_hash,origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, item, srow, "code", "worked", now,
             "high", "stage_a", aid, "h", "d", now, now))
        out = tokencost.tokens_for_work_item(self.conn, item)
        self.assertEqual(out["totals"]["input_tokens"], 100)
        self.assertEqual(out["totals"]["output_tokens"], 20)
        self.assertAlmostEqual(out["cost_usd"], 100 / 1e6 * 5 + 20 / 1e6 * 25, places=6)
        self.assertEqual(out["models"][0]["model"], "claude-opus-5")


from work_ledger.query import daily as q_daily, render as q_render, status as q_status


class TestSurfacing(_TokenCase):
    def setUp(self):
        super().setUp()
        now_dt = timeutil.parse_iso(timeutil.utc_now_iso())
        from datetime import timedelta
        started = (now_dt - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self._ingest([
            _record("u1", model="claude-opus-5",
                    usage={"input_tokens": 1_200_000, "output_tokens": 85_300}),
        ])
        now = timeutil.utc_now_iso()
        self.conn.execute(
            "INSERT INTO connections(id,name,adapter,origin_device_id,created_at,updated_at)"
            " VALUES ('c1','j','jira-rest','d',?,?)", (now, now))
        from work_ledger import entities as ent
        client = ent.upsert_client(self.conn, "Acme")
        self.item = wi.upsert_external(self.conn, "c1", "AB-76", title="t",
                                       client_id=client)
        srow = self.conn.execute(
            "SELECT id FROM sessions WHERE session_id='s1'").fetchone()["id"]
        aid = ids.new_id()
        self.conn.execute(
            "INSERT INTO activities(id,work_item_id,client_id,session_row_id,kind,summary,"
            "started_at,ended_at,confidence,source,idempotency_key,source_hash,"
            "origin_device_id,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, self.item, client, srow, "code", "worked", started, now,
             "high", "stage_a", aid, "h", "d", now, now))

    def test_status_carries_and_renders_claude_usage(self):
        report = q_status.status_report(
            self.conn, "AB-76", tz="America/Chicago", rollover_hour=3)
        self.assertEqual(report["claude"]["totals"]["input_tokens"], 1_200_000)
        self.assertAlmostEqual(report["claude"]["cost_usd"],
                               1.2 * 5 + 0.0853 * 25, places=3)
        text = q_render.render_report(report, fmt="text")
        self.assertIn("Claude usage: 1.2M in / 85k out (~$8.13)", text)

    def test_day_report_carries_client_claude_totals(self):
        workday = timeutil.workday_of(timeutil.utc_now_iso(), "America/Chicago", 3)
        report = q_daily.day_report(
            self.conn, workday, tz="America/Chicago", rollover_hour=3)
        client = report["clients"][0]
        self.assertEqual(client["claude"]["totals"]["input_tokens"], 1_200_000)
        text = q_render.render_report(report, fmt="text")
        self.assertIn("1.2M in", text)


if __name__ == "__main__":
    unittest.main()
