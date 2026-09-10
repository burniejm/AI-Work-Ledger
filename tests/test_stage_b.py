import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from work_ledger import cli
from work_ledger import config as config_mod
from work_ledger import doctor, entities, remaining
from work_ledger.capture import events as ev
from work_ledger.capture import sessions as S
from work_ledger.db import connection as dbc
from work_ledger.db import migrations as m
from work_ledger.infer import stage_a, stage_b

STUB_DIR = str(Path(__file__).parent / "fixtures" / "bin")


class _StageBCase(unittest.TestCase):
    """Shared fixture: a stubbed `claude` on PATH, a private WORK_LEDGER_HOME,
    one session and one commit event.

    PATH and the STUB_CLAUDE_MODE switch are restored in tearDown -- the whole
    suite runs in one process and `test_missing_claude_disables_stage_b`
    deliberately blanks PATH, which would otherwise leak into every module
    that discover() loads after this one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("WORK_LEDGER_HOME")
        self._old_path = os.environ["PATH"]
        self._old_mode = os.environ.get("STUB_CLAUDE_MODE")
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        os.environ["PATH"] = STUB_DIR + os.pathsep + os.environ["PATH"]
        os.environ.pop("WORK_LEDGER_NESTED", None)
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite"))
        m.apply_all(self.conn)
        S.upsert(self.conn, session_id="s1", cwd="/tmp/x",
                 occurred_at="2026-08-30T12:00:00.000Z")
        ev.append_source_event(self.conn, session_id="s1", event_type="commit",
                               occurred_at="2026-08-30T12:05:00.000Z",
                               payload={"sha": "a1", "subject": "AB-76 fix upload retry"},
                               idem=ev.idempotency_key("s1", "commit", "a1"))

    def tearDown(self):
        self.conn.close()
        os.environ["PATH"] = self._old_path
        os.environ.pop("WORK_LEDGER_NESTED", None)
        if self._old_mode is None:
            os.environ.pop("STUB_CLAUDE_MODE", None)
        else:
            os.environ["STUB_CLAUDE_MODE"] = self._old_mode
        if self._old_home is None:
            os.environ.pop("WORK_LEDGER_HOME", None)
        else:
            os.environ["WORK_LEDGER_HOME"] = self._old_home
        self.tmp.cleanup()


class TestStageB(_StageBCase):
    def test_happy_path_creates_a_capped_activity(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        out = stage_b.run(self.conn, "s1", force=True)
        self.assertTrue(out["ran"], out)
        row = self.conn.execute(
            "SELECT confidence, source FROM activities WHERE source='stage_b'").fetchone()
        self.assertEqual(row["source"], "stage_b")
        # never high on Stage B alone (Global Constraints / DESIGN 6 override 2)
        self.assertIn(row["confidence"], ("medium", "low"))

    def test_rerun_with_the_same_bundle_is_idempotent(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", force=True)
        before = self.conn.execute("SELECT count(*) FROM activities").fetchone()[0]
        stage_b.run(self.conn, "s1", force=True)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM activities").fetchone()[0], before)

    def test_garbage_output_fails_softly(self):
        os.environ["STUB_CLAUDE_MODE"] = "garbage"
        out = stage_b.run(self.conn, "s1", force=True)
        self.assertFalse(out["ran"])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM capture_checkpoints WHERE hook_event='stage_b' "
            "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()[0], "failed")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM activities WHERE source='stage_b'").fetchone()[0], 0)

    def test_schema_violation_is_rejected(self):
        os.environ["STUB_CLAUDE_MODE"] = "badschema"
        with self.assertRaises(stage_b.StageBSchemaError):
            stage_b.validate_output(json.dumps({"schema_version": 1, "activities": [
                {"kind": "code", "summary": "x", "confidence": "certain",
                 "evidence_event_ids": []}]}))
        self.assertFalse(stage_b.run(self.conn, "s1", force=True)["ran"])

    def test_process_failure_fails_softly(self):
        os.environ["STUB_CLAUDE_MODE"] = "fail"
        self.assertFalse(stage_b.run(self.conn, "s1", force=True)["ran"])

    def test_nested_sentinel_blocks_the_run(self):
        os.environ["WORK_LEDGER_NESTED"] = "1"
        try:
            ok, reason = stage_b.should_run(self.conn, "s1",
                                            now="2026-08-30T13:00:00.000Z", force=True)
            self.assertFalse(ok)
            self.assertIn("nested", reason.lower())
        finally:
            del os.environ["WORK_LEDGER_NESTED"]

    def test_missing_claude_disables_stage_b(self):
        os.environ["PATH"] = "/nonexistent"
        ok, reason = stage_b.should_run(self.conn, "s1",
                                        now="2026-08-30T13:00:00.000Z", force=True)
        self.assertFalse(ok)
        self.assertIn("claude", reason.lower())

    def test_rate_limit_blocks_a_second_run_inside_the_window(self):
        # Ruling 30(e): inject `now` into BOTH sides so the test is not
        # wall-clock dependent. min_interval_minutes defaults to 20;
        # 12:50 -> 13:00 is 10 minutes, inside the window.
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        first = stage_b.run(self.conn, "s1", now="2026-08-30T12:50:00.000Z")
        self.assertTrue(first["ran"], first)
        self.assertEqual(self.conn.execute(
            "SELECT started_at FROM capture_checkpoints "
            "WHERE hook_event='stage_b' AND status='ok'").fetchone()[0],
            "2026-08-30T12:50:00.000Z")
        ok, reason = stage_b.should_run(self.conn, "s1", now="2026-08-30T13:00:00.000Z")
        self.assertFalse(ok)
        self.assertIn("rate", reason.lower())

    def test_rate_limit_expires_after_the_window(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", now="2026-08-30T12:50:00.000Z")
        ok, _ = stage_b.should_run(self.conn, "s1", now="2026-08-30T13:20:00.000Z")
        self.assertTrue(ok)

    def test_force_bypasses_only_the_rate_limit(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", now="2026-08-30T12:50:00.000Z")
        blocked, _ = stage_b.should_run(self.conn, "s1", now="2026-08-30T12:55:00.000Z")
        self.assertFalse(blocked)
        forced, _ = stage_b.should_run(self.conn, "s1", now="2026-08-30T12:55:00.000Z",
                                       force=True)
        self.assertTrue(forced)

    def test_disabled_by_config_blocks_even_with_force(self):
        config_mod.save({"stage_b": {"enabled": False}})
        ok, reason = stage_b.should_run(self.conn, "s1",
                                        now="2026-08-30T13:00:00.000Z", force=True)
        self.assertFalse(ok)
        self.assertIn("enabled", reason.lower())

    def test_bundle_is_redacted_and_bounded(self):
        ev.append_source_event(
            self.conn, session_id="s1", event_type="tool_use",
            occurred_at="2026-08-30T12:06:00.000Z",
            payload={"stdout": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 " + "x" * 50_000},
            idem=ev.idempotency_key("s1", "t", "big"))
        bundle, h = stage_b.build_bundle(self.conn, "s1", max_bytes=8000)
        blob = json.dumps(bundle)
        self.assertLessEqual(len(blob.encode()), 8000)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", blob)
        self.assertEqual(len(h), 64)

    def test_bundle_drops_the_oldest_low_value_events_first(self):
        for i in range(30):
            ev.append_source_event(
                self.conn, session_id="s1", event_type="tool_use",
                occurred_at=f"2026-08-30T12:{10 + i:02d}:00.000Z",
                payload={"note": "n" * 400}, idem=ev.idempotency_key("s1", "t", str(i)))
        bundle, _h = stage_b.build_bundle(self.conn, "s1", max_bytes=4000)
        self.assertLessEqual(len(json.dumps(bundle).encode()), 4000)
        kinds = [e["event_type"] for e in bundle]
        # the high-value commit survives; the oldest tool_use rows are gone
        self.assertIn("commit", kinds)
        kept = [e["occurred_at"] for e in bundle if e["event_type"] == "tool_use"]
        self.assertTrue(all(t > "2026-08-30T12:10:00.000Z" for t in kept), kept)

    def test_bundle_hash_is_stable_and_changes_with_content(self):
        _b1, h1 = stage_b.build_bundle(self.conn, "s1", max_bytes=60000)
        _b2, h2 = stage_b.build_bundle(self.conn, "s1", max_bytes=60000)
        self.assertEqual(h1, h2)
        ev.append_source_event(self.conn, session_id="s1", event_type="commit",
                               occurred_at="2026-08-30T12:09:00.000Z",
                               payload={"sha": "b2", "subject": "another"},
                               idem=ev.idempotency_key("s1", "commit", "b2"))
        _b3, h3 = stage_b.build_bundle(self.conn, "s1", max_bytes=60000)
        self.assertNotEqual(h1, h3)

    def test_prompt_names_the_schema_and_demands_json_only(self):
        bundle, _h = stage_b.build_bundle(self.conn, "s1", max_bytes=60000)
        prompt = stage_b.build_prompt(bundle, context={"session_id": "s1",
                                                       "cwd": "/tmp/x",
                                                       "git_branch": None})
        self.assertIn("schema_version", prompt)
        self.assertIn("evidence_event_ids", prompt)
        self.assertIn("JSON", prompt)
        self.assertIn("only", prompt.lower())

    def test_validate_output_extracts_the_first_balanced_object(self):
        text = ('Sure! Here is the JSON:\n```json\n'
                '{"schema_version": 1, "activities": []}\n```\nHope that helps.')
        parsed = stage_b.validate_output(text)
        self.assertEqual(parsed["activities"], [])

    def test_validate_output_ignores_unknown_keys(self):
        parsed = stage_b.validate_output(json.dumps({
            "schema_version": 1, "nonsense": True, "activities": [
                {"kind": "test", "summary": "ran the suite", "confidence": "low",
                 "evidence_event_ids": [], "made_up": 7}]}))
        self.assertNotIn("nonsense", parsed)
        self.assertNotIn("made_up", parsed["activities"][0])

    def test_validate_output_skips_a_stray_brace_in_the_prose(self):
        # Found in live verification: the real CLI answered with prose that
        # contained a `{` before the JSON, and scanning only from the first
        # brace reported "unbalanced JSON object" and took Stage B offline.
        text = ('The schema wants a { shaped object, so here it is:\n'
                '{"schema_version": 1, "activities": []}')
        self.assertEqual(stage_b.validate_output(text)["activities"], [])

    def test_validate_output_still_rejects_text_with_no_complete_object(self):
        with self.assertRaises(stage_b.StageBSchemaError):
            stage_b.validate_output('here is a { and another { and no close')

    def test_validate_output_picks_the_object_that_carries_schema_version(self):
        # Also from live verification: a preamble object made the extractor
        # answer "schema_version must be 1, got None" and fail the whole pass.
        text = ('{"note": "here is my plan"}\n'
                '{"schema_version": 1, "activities": ['
                '{"kind": "test", "summary": "ran it", "confidence": "low",'
                ' "evidence_event_ids": []}]}')
        parsed = stage_b.validate_output(text)
        self.assertEqual(parsed["activities"][0]["summary"], "ran it")

    def test_validate_output_repairs_a_document_missing_its_closing_brace(self):
        # Measured against the real CLI: a Stage B answer of a few hundred
        # characters arrives missing the ROOT object's final `}` -- everything
        # inside it closes, the document does not. Without this the whole pass
        # was lost to "unbalanced JSON object".
        good = json.dumps({"schema_version": 1, "activities": [
            {"kind": "code", "summary": "Created notes.txt and ran wc -l",
             "outcome": None, "confidence": "high", "evidence_event_ids": [],
             "remaining": [], "blockers": [], "decisions": []}]})
        self.assertTrue(good.endswith("}"))
        parsed = stage_b.validate_output(good[:-1])          # drop the last brace
        self.assertEqual(parsed["activities"][0]["summary"],
                         "Created notes.txt and ran wc -l")

    def test_the_repair_never_invents_content_inside_a_truncated_string(self):
        # Cut mid-summary: closing the string would fabricate the sentence the
        # model did not finish, and the length caps would accept it happily.
        truncated = ('{"schema_version": 1, "activities": [{"kind": "code", '
                     '"summary": "Created notes.txt and then')
        with self.assertRaises(stage_b.StageBSchemaError):
            stage_b.validate_output(truncated)

    def test_the_repair_does_not_rescue_genuinely_malformed_json(self):
        with self.assertRaises(stage_b.StageBSchemaError):
            stage_b.validate_output('{"schema_version": 1, "activities": [}')

    def test_validate_output_reports_on_the_answer_not_the_preamble(self):
        # No candidate carries schema_version, so the error must describe the
        # first real object rather than something further along.
        with self.assertRaisesRegex(stage_b.StageBSchemaError, "schema_version"):
            stage_b.validate_output('{"note": "no version here"}')

    def test_validate_output_rejects_unknown_evidence_ids(self):
        text = json.dumps({"schema_version": 1, "activities": [
            {"kind": "code", "summary": "s", "confidence": "low",
             "evidence_event_ids": ["not-a-real-id"]}]})
        with self.assertRaises(stage_b.StageBSchemaError):
            stage_b.validate_output(text, known_event_ids={"real-id"})

    def test_validate_output_rejects_an_over_long_summary(self):
        text = json.dumps({"schema_version": 1, "activities": [
            {"kind": "code", "summary": "x" * 401, "confidence": "low",
             "evidence_event_ids": []}]})
        with self.assertRaises(stage_b.StageBSchemaError):
            stage_b.validate_output(text)

    def test_validate_output_rejects_a_wrong_schema_version(self):
        with self.assertRaises(stage_b.StageBSchemaError):
            stage_b.validate_output(json.dumps({"schema_version": 2, "activities": []}))

    def test_a_declined_guard_checkpoints_as_skipped_not_failed(self):
        # IMPORTANT 1: rate limited / disabled / nothing captured are ordinary
        # outcomes. Recording them as `failed` made doctor warn about a
        # healthy system.
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", now="2026-08-30T12:50:00.000Z")
        out = stage_b.run(self.conn, "s1", now="2026-08-30T12:55:00.000Z")
        self.assertFalse(out["ran"])
        self.assertEqual(out["status"], "skipped")
        row = self.conn.execute(
            "SELECT status, detail FROM capture_checkpoints WHERE hook_event='stage_b' "
            "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertIn("rate limited", row["detail"])

    def test_an_empty_session_is_skipped_not_failed(self):
        S.upsert(self.conn, session_id="empty", cwd="/tmp/x",
                 occurred_at="2026-08-30T12:00:00.000Z")
        out = stage_b.run(self.conn, "empty", force=True)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM capture_checkpoints WHERE hook_event='stage_b' "
            "AND session_id='empty'").fetchone()[0], "skipped")

    def test_a_real_breakage_still_checkpoints_as_failed(self):
        for mode in ("fail", "garbage", "badschema"):
            with self.subTest(mode=mode):
                os.environ["STUB_CLAUDE_MODE"] = mode
                out = stage_b.run(self.conn, "s1", force=True)
                self.assertEqual(out["status"], "failed")

    def test_doctor_treats_a_skipped_pass_as_healthy_and_a_failure_as_a_warning(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", now="2026-08-30T12:50:00.000Z")
        stage_b.run(self.conn, "s1", now="2026-08-30T12:55:00.000Z")   # rate limited
        state, detail = doctor._check_stage_b(self.conn)
        self.assertEqual(state, "ok", detail)
        os.environ["STUB_CLAUDE_MODE"] = "garbage"
        stage_b.run(self.conn, "s1", force=True)
        state, detail = doctor._check_stage_b(self.conn)
        self.assertEqual(state, "warn", detail)

    def test_shrinking_a_large_bundle_is_linear_not_quadratic(self):
        # IMPORTANT 5: the old loop re-encoded the whole bundle per dropped
        # event. 1500 events that must shrink to ~nothing is the shape that
        # made that O(n^2); it has to stay well under a second.
        for i in range(1500):
            ev.append_source_event(
                self.conn, session_id="s1", event_type="tool_use",
                occurred_at=f"2026-08-30T13:{i // 60:02d}:{i % 60:02d}.000Z",
                payload={"note": "n" * 300}, idem=ev.idempotency_key("s1", "big", str(i)))
        t0 = time.monotonic()
        bundle, _h = stage_b.build_bundle(self.conn, "s1", max_bytes=5000)
        elapsed = time.monotonic() - t0
        self.assertLessEqual(len(json.dumps(bundle).encode()), 5000)
        self.assertLess(elapsed, 1.0, f"build_bundle took {elapsed:.2f}s")

    def test_run_builds_the_bundle_exactly_once(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        calls = []
        real = stage_b.build_bundle

        def counting(*args, **kwargs):
            calls.append(1)
            return real(*args, **kwargs)

        stage_b.build_bundle = counting
        try:
            out = stage_b.run(self.conn, "s1", force=True)
        finally:
            stage_b.build_bundle = real
        self.assertTrue(out["ran"], out)
        self.assertEqual(len(calls), 1, f"build_bundle called {len(calls)} times")

    def test_spawn_returns_immediately_and_does_not_block(self):
        os.environ["STUB_CLAUDE_MODE"] = "slow"
        t0 = time.monotonic()
        stage_b.spawn("s1")
        self.assertLess(time.monotonic() - t0, 1.0)


class TestStageBMerge(_StageBCase):
    """The Controller ruling carried from Task 28: remaining/blockers are
    routed through `infer.dedupe.resolve` on both the Stage B path and the
    Stage-A-only path, exactly once per processing pass."""

    def test_stage_b_remaining_is_routed_through_dedupe(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", force=True)
        rows = self.conn.execute(
            "SELECT title, source, kind FROM remaining_items").fetchall()
        self.assertEqual([r["title"] for r in rows], ["Retest on replay hardware"])
        self.assertEqual(rows[0]["source"], "stage_b")

    def test_rerunning_reinforces_rather_than_duplicating(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", force=True)
        stage_b.run(self.conn, "s1", force=True)
        rows = self.conn.execute(
            "SELECT mention_count FROM remaining_items").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["mention_count"], 2)

    def test_stage_b_decisions_are_persisted_once(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", force=True)
        stage_b.run(self.conn, "s1", force=True)
        rows = self.conn.execute("SELECT statement, source FROM decisions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "stage_b")

    def _commit_with_a_todo(self):
        ev.append_source_event(
            self.conn, session_id="s1", event_type="commit",
            occurred_at="2026-08-30T12:07:00.000Z",
            payload={"sha": "c3", "subject": "AB-76 wire the retry",
                     "body": "TODO: benchmark the retry loop\nBlocked: waiting on hardware"},
            idem=ev.idempotency_key("s1", "commit", "c3"))

    def test_stage_a_remaining_is_persisted_when_stage_b_cannot_run(self):
        self._commit_with_a_todo()
        stage_a.run(self.conn, "s1")
        os.environ["STUB_CLAUDE_MODE"] = "fail"
        out = stage_b.run(self.conn, "s1", force=True)
        self.assertFalse(out["ran"])
        titles = {r["title"] for r in self.conn.execute(
            "SELECT title FROM remaining_items").fetchall()}
        self.assertIn("benchmark the retry loop", titles)
        self.assertIn("waiting on hardware", titles)
        kinds = {r["title"]: r["kind"] for r in self.conn.execute(
            "SELECT title, kind FROM remaining_items").fetchall()}
        self.assertEqual(kinds["waiting on hardware"], "blocker")
        self.assertEqual({r["source"] for r in self.conn.execute(
            "SELECT source FROM remaining_items").fetchall()}, {"stage_a"})

    def test_the_stage_a_only_pass_runs_exactly_once_over_the_same_evidence(self):
        self._commit_with_a_todo()
        stage_a.run(self.conn, "s1")
        os.environ["STUB_CLAUDE_MODE"] = "fail"
        stage_b.run(self.conn, "s1", force=True)
        counts = {r["title"]: r["mention_count"] for r in self.conn.execute(
            "SELECT title, mention_count FROM remaining_items").fetchall()}
        stage_b.run(self.conn, "s1", force=True)
        again = {r["title"]: r["mention_count"] for r in self.conn.execute(
            "SELECT title, mention_count FROM remaining_items").fetchall()}
        self.assertEqual(counts, again)

    def test_stage_b_never_overwrites_a_high_confidence_stage_a_attribution(self):
        self._commit_with_a_todo()
        stage_a.run(self.conn, "s1")
        client_id = entities.upsert_client(self.conn, "Acme")
        self.conn.execute(
            "UPDATE activities SET confidence='high', confidence_score=100, client_id=? "
            "WHERE source='stage_a'", (client_id,))
        before = self.conn.execute(
            "SELECT id, client_id, confidence, summary FROM activities "
            "WHERE source='stage_a'").fetchall()
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        stage_b.run(self.conn, "s1", force=True)
        after = self.conn.execute(
            "SELECT id, client_id, confidence, summary FROM activities "
            "WHERE source='stage_a'").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])


class TestStageBEvidenceAndCorrections(_StageBCase):
    """Review findings IMPORTANT 2 (a correction's attribution survives a
    re-merge) and IMPORTANT 3 (cited evidence is actually linked)."""

    def test_cited_events_become_activity_evidence_rows(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        # The stub cites nothing, so drive merge directly with the real ids.
        bundle, bundle_hash = stage_b.build_bundle(self.conn, "s1", max_bytes=60000)
        cited = [entry["id"] for entry in bundle]
        parsed = stage_b.validate_output(json.dumps({"schema_version": 1, "activities": [
            {"kind": "code", "summary": "Fixed the upload retry loop",
             "confidence": "medium", "evidence_event_ids": cited}]}),
            known_event_ids=set(cited))
        out = stage_b.merge(self.conn, "s1", parsed, bundle_hash, bundle=bundle)
        self.assertEqual(out["evidence"], len(cited))
        rows = self.conn.execute(
            "SELECT ae.source_event_id, ae.evidence_type FROM activity_evidence ae "
            "JOIN activities a ON a.id = ae.activity_id WHERE a.source='stage_b'").fetchall()
        self.assertEqual({r["source_event_id"] for r in rows}, set(cited))
        self.assertEqual({r["evidence_type"] for r in rows}, {"commit"})
        # every link points at a real source_events row
        for row in rows:
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM source_events WHERE id=?", (row["source_event_id"],)).fetchone())

    def test_evidence_links_are_not_duplicated_on_a_re_merge(self):
        bundle, bundle_hash = stage_b.build_bundle(self.conn, "s1", max_bytes=60000)
        cited = [entry["id"] for entry in bundle]
        parsed = stage_b.validate_output(json.dumps({"schema_version": 1, "activities": [
            {"kind": "code", "summary": "Fixed the upload retry loop",
             "confidence": "medium", "evidence_event_ids": cited}]}),
            known_event_ids=set(cited))
        stage_b.merge(self.conn, "s1", parsed, bundle_hash, bundle=bundle)
        stage_b.merge(self.conn, "s1", parsed, bundle_hash, bundle=bundle)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM activity_evidence").fetchone()[0], len(cited))

    def test_a_correction_keeps_its_attribution_across_a_re_merge(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        out = stage_b.run(self.conn, "s1", force=True)
        self.assertTrue(out["ran"], out)
        activity_id = self.conn.execute(
            "SELECT id FROM activities WHERE source='stage_b'").fetchone()[0]
        bundle_hash = out["bundle_hash"]

        # A user moves the activity to a client/project of their own. Note that
        # `corrections.move_activity` leaves `source` alone, which is exactly
        # why the UPDATE must not touch attribution columns.
        client_id = entities.upsert_client(self.conn, "Acme")
        project_id = entities.upsert_project(self.conn, client_id, "Replay")
        self.conn.execute("UPDATE activities SET client_id=?, project_id=? WHERE id=?",
                          (client_id, project_id, activity_id))

        # Re-merge the SAME bundle with different narrative text, so the
        # source_hash changes and the UPDATE branch runs.
        parsed = stage_b.validate_output(json.dumps({"schema_version": 1, "activities": [
            {"kind": "investigation", "summary": "A completely different summary",
             "confidence": "low", "evidence_event_ids": []}]}))
        merged = stage_b.merge(self.conn, "s1", parsed, bundle_hash)
        self.assertEqual(merged["updated"], 1)

        row = self.conn.execute(
            "SELECT client_id, project_id, work_item_id, summary FROM activities WHERE id=?",
            (activity_id,)).fetchone()
        self.assertEqual(row["client_id"], client_id)      # attribution survived
        self.assertEqual(row["project_id"], project_id)
        self.assertEqual(row["summary"], "A completely different summary")  # narrative did not

    def test_a_discarded_ticket_hint_is_recorded_in_the_confidence_reason(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"      # the stub hints AB-76
        stage_b.run(self.conn, "s1", force=True)
        reason = self.conn.execute(
            "SELECT confidence_reason FROM activities WHERE source='stage_b'").fetchone()[0]
        self.assertIn("AB-76", reason)
        self.assertIn("discarded", reason.lower())

    def test_a_reported_blocker_is_created_in_the_blocked_state(self):
        bundle, bundle_hash = stage_b.build_bundle(self.conn, "s1", max_bytes=60000)
        parsed = stage_b.validate_output(json.dumps({"schema_version": 1, "activities": [
            {"kind": "code", "summary": "wired the retry", "confidence": "medium",
             "evidence_event_ids": [],
             "remaining": [{"title": "waiting on the vendor", "state": "waiting"}],
             "blockers": [{"title": "hardware has not shipped"}]}]}))
        out = stage_b.merge(self.conn, "s1", parsed, bundle_hash, bundle=bundle)
        states = {r["title"]: (r["state"], r["kind"]) for r in self.conn.execute(
            "SELECT title, state, kind FROM remaining_items").fetchall()}
        self.assertEqual(states["hardware has not shipped"], ("blocked", "blocker"))
        self.assertEqual(states["waiting on the vendor"], ("waiting", "waiting"))
        # both states are still "open" for reporting purposes (Ruling 28)
        self.assertTrue(set(s for s, _k in states.values()) <= set(remaining.OPEN_STATES))
        self.assertEqual(out["remaining_actions"], {"created": 2})

    def test_the_stage_a_fence_is_claimed_before_the_resolve_loop(self):
        # IMPORTANT minor: claim-then-do. A crash mid-loop must not leave the
        # fence unwritten, or the next pass re-resolves and inflates mentions.
        ev.append_source_event(
            self.conn, session_id="s1", event_type="commit",
            occurred_at="2026-08-30T12:07:00.000Z",
            payload={"sha": "c3", "subject": "AB-76 wire the retry",
                     "body": "TODO: benchmark the retry loop"},
            idem=ev.idempotency_key("s1", "commit", "c3"))
        stage_a.run(self.conn, "s1")

        real_resolve = stage_b.dedupe.resolve
        seen = {}

        def exploding(conn, **kwargs):
            seen["fence"] = conn.execute(
                "SELECT count(*) FROM capture_checkpoints "
                "WHERE hook_event='stage_a_remaining'").fetchone()[0]
            raise RuntimeError("boom")

        stage_b.dedupe.resolve = exploding
        try:
            with self.assertRaises(RuntimeError):
                stage_b.persist_stage_a_remaining(self.conn, "s1",
                                                  now="2026-08-30T13:00:00.000Z")
        finally:
            stage_b.dedupe.resolve = real_resolve
        self.assertEqual(seen["fence"], 1, "the fence must be claimed before the loop")


class TestStageBSpawnedCli(_StageBCase):
    """IMPORTANT 4: pin the spawn -> CLI sentinel-consumption path.

    `spawn` hands the child `WORK_LEDGER_NESTED=1` (per the brief) *and*
    `WORK_LEDGER_STAGE_B_SPAWNED=1`; the CLI consumes the sentinel only when
    the companion marker is there. Without this test nothing proves the Stop
    hook's child can actually run -- and nothing proves a sentinel from
    anyone else still refuses.
    """

    def setUp(self):
        super().setUp()
        # `cli.main` opens the default database, not this case's t.sqlite.
        self.default = dbc.connect()
        m.apply_all(self.default)
        S.upsert(self.default, session_id="s1", cwd="/tmp/x",
                 occurred_at="2026-08-30T12:00:00.000Z")
        ev.append_source_event(self.default, session_id="s1", event_type="commit",
                               occurred_at="2026-08-30T12:05:00.000Z",
                               payload={"sha": "a1", "subject": "AB-76 fix upload retry"},
                               idem=ev.idempotency_key("s1", "commit", "a1"))

    def tearDown(self):
        self.default.close()
        super().tearDown()

    def _run_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["stage-b", "--session", "s1"])
        return rc, buf.getvalue()

    def test_the_spawned_child_consumes_its_own_sentinel_and_runs(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        os.environ[stage_b.NESTED_ENV] = "1"
        os.environ[stage_b.SPAWNED_ENV] = "1"
        try:
            rc, _out = self._run_cli()
        finally:
            os.environ.pop(stage_b.NESTED_ENV, None)
            os.environ.pop(stage_b.SPAWNED_ENV, None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.default.execute(
            "SELECT count(*) FROM activities WHERE source='stage_b'").fetchone()[0], 1)
        self.assertEqual(self.default.execute(
            "SELECT status FROM capture_checkpoints WHERE hook_event='stage_b' "
            "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()[0], "ok")
        # the sentinel is consumed, not left lying around for the next command
        self.assertIsNone(os.environ.get(stage_b.NESTED_ENV))

    def test_a_sentinel_from_anyone_else_still_refuses(self):
        os.environ["STUB_CLAUDE_MODE"] = "ok"
        os.environ[stage_b.NESTED_ENV] = "1"          # no SPAWNED marker
        try:
            rc, out = self._run_cli()
        finally:
            os.environ.pop(stage_b.NESTED_ENV, None)
        self.assertEqual(rc, 0)
        self.assertIn("nested", out.lower())
        self.assertEqual(self.default.execute(
            "SELECT count(*) FROM activities WHERE source='stage_b'").fetchone()[0], 0)
        self.assertEqual(self.default.execute(
            "SELECT status FROM capture_checkpoints WHERE hook_event='stage_b' "
            "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()[0], "skipped")


if __name__ == "__main__":
    unittest.main()
