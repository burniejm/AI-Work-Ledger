import os, tempfile, unittest
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import entities as e, work_items as wi, textnorm as tn, remaining
from work_ledger.infer import dedupe as d

class TestSimilarity(unittest.TestCase):
    def test_similarity_is_jaccard_over_normalized_tokens(self):
        # {retest, upload, recovery} vs {retest, upload, recovery, path}
        #   intersection 3, union 4 -> 0.75
        self.assertAlmostEqual(
            d.similarity("Retest upload recovery", "Retest the upload recovery path"), 0.75, places=6)
        self.assertGreater(d.similarity("Retest upload recovery", "Retest the upload recovery path"), 0.6)
        # {retest, upload, recovery} vs {write, release, notes} -> 0 / 6 = 0.0
        self.assertEqual(d.similarity("Retest upload recovery", "Write the release notes"), 0.0)
        # dedupe borrows its tokens from textnorm; it defines no stopword list (Ruling 23)
        self.assertIs(d.normalize_key, tn.normalize_key)
        self.assertFalse(hasattr(d, "STOPWORDS"))
        self.assertFalse(hasattr(d, "STEM_MAP"))

    def test_material_difference_signals(self):
        self.assertIsNotNone(d.material_difference("Fix AB-76 upload", "Fix AB-326 upload"))
        self.assertIsNotNone(d.material_difference("Fix src/a.go parser", "Fix src/b.go parser"))
        self.assertIsNotNone(d.material_difference("Upload works on hardware", "Upload does not work on hardware"))
        self.assertIsNone(d.material_difference("Retest upload", "Retest the upload"))

class TestResolve(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = dbc.connect(os.path.join(self.tmp.name, "t.sqlite")); m.apply_all(self.conn)
        # Ruling 2: real rows, because remaining_items.work_item_id REFERENCES work_items(id)
        # and PRAGMA foreign_keys=ON. Dangling "w1"/"w2" raised IntegrityError on every call.
        self.cl = e.upsert_client(self.conn, "Acme")
        self.pr = e.upsert_project(self.conn, self.cl, "Replay")
        self.w1 = wi.create_local(self.conn, "Upload recovery", client_id=self.cl, project_id=self.pr)
        self.w2 = wi.create_local(self.conn, "Replay hardware", client_id=self.cl, project_id=self.pr)
        self.kw = dict(work_item_id=self.w1, client_id=self.cl, project_id=self.pr,
                       confidence="high", source="stage_a")
    def tearDown(self):
        self.tmp.cleanup()

    def test_first_mention_creates(self):
        rid, action = d.resolve(self.conn, title="Retest upload recovery",
                                occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        self.assertEqual(action, "created")
        self.assertEqual(self.conn.execute(
            "SELECT work_item_id FROM remaining_items WHERE id=?", (rid,)).fetchone()[0], self.w1)

    def test_identical_mention_reinforces(self):
        d.resolve(self.conn, title="Retest upload recovery", occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        rid, action = d.resolve(self.conn, title="Retest the upload recovery",
                                occurred_at="2026-08-29T12:00:00.000Z", **self.kw)
        # normalize_key drops "the": both sides are "recovery-retest-upload" -> equal keys
        self.assertEqual(action, "reinforced")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM remaining_items").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT mention_count FROM remaining_items").fetchone()[0], 2)

    def test_middling_similarity_creates_and_flags_a_possible_duplicate(self):
        # Ruling 4: this fixture genuinely lands inside the 0.60-0.85 band.
        #
        #   "Retest upload recovery on replay hardware"
        #       -> {retest, upload, recovery, replay, hardware}          (5; "on" is a stopword)
        #   "Retest the upload recovery on replay hardware after reboot"
        #       -> {retest, upload, recovery, replay, hardware, after, reboot}
        #                                                                (7; "the"/"on" are stopwords)
        #   intersection = 5, union = 7  ->  Jaccard = 5 / 7 = 0.714...
        #
        # 0.60 <= 0.714 < 0.85, and material_difference() is None (same ticket keys,
        # no file paths, no negation), so DESIGN 7 says: create a new item AND open a
        # possible_duplicate contradiction. The previous fixture scored 3/8 = 0.375,
        # which lands in the "< 0.60" band and would silently have created without a flag.
        a = "Retest upload recovery on replay hardware"
        b = "Retest the upload recovery on replay hardware after reboot"
        self.assertAlmostEqual(d.similarity(a, b), 5 / 7, places=6)
        self.assertGreaterEqual(d.similarity(a, b), d.DUPLICATE_BAND[0])
        self.assertLess(d.similarity(a, b), d.DUPLICATE_BAND[1])
        self.assertIsNone(d.material_difference(a, b))

        d.resolve(self.conn, title=a, occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        rid, action = d.resolve(self.conn, title=b, occurred_at="2026-08-29T12:00:00.000Z", **self.kw)
        self.assertEqual(action, "created_with_duplicate_flag")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM remaining_items").fetchone()[0], 2)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM contradictions WHERE rule='possible_duplicate' AND status='open'").fetchone()[0], 1)

    def test_band_boundaries_are_named_constants(self):
        # 0.85 band boundary: {retest, upload, recovery} vs {retest, upload, recovery, path}
        # is 3/4 = 0.75, which is BELOW REINFORCE_THRESHOLD, so it must land in the
        # duplicate band, not reinforce. Equality of normalized keys is the only
        # reinforcement path exercised by test_identical_mention_reinforces.
        self.assertEqual(d.REINFORCE_THRESHOLD, 0.85)
        self.assertAlmostEqual(d.similarity("Retest upload recovery",
                                            "Retest the upload recovery path"), 0.75, places=6)
        d.resolve(self.conn, title="Retest upload recovery",
                  occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        _, action = d.resolve(self.conn, title="Retest the upload recovery path",
                              occurred_at="2026-08-29T12:00:00.000Z", **self.kw)
        self.assertEqual(action, "created_with_duplicate_flag")

    def test_material_difference_never_merges(self):
        d.resolve(self.conn, title="Fix AB-76 upload path", occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        rid, action = d.resolve(self.conn, title="Fix AB-326 upload path",
                                occurred_at="2026-08-29T12:00:00.000Z", **self.kw)
        self.assertEqual(action, "created")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM remaining_items").fetchone()[0], 2)

    def test_different_work_items_do_not_collide(self):
        d.resolve(self.conn, title="Retest upload", occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        kw2 = {**self.kw, "work_item_id": self.w2}
        rid, action = d.resolve(self.conn, title="Retest upload", occurred_at="2026-08-28T12:00:00.000Z", **kw2)
        self.assertEqual(action, "created")
        self.assertEqual(self.conn.execute(
            "SELECT work_item_id FROM remaining_items WHERE id=?", (rid,)).fetchone()[0], self.w2)

    def test_resolved_item_reopens_on_exact_key_match(self):
        rid, _ = d.resolve(self.conn, title="Retest upload recovery",
                            occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        remaining.transition(self.conn, rid, "completed", occurred_at="2026-08-29T12:00:00.000Z",
                              source="manual", confidence="high", explanation="done")
        rid2, action = d.resolve(self.conn, title="Retest the upload recovery",
                                  occurred_at="2026-08-30T12:00:00.000Z", **self.kw)
        self.assertEqual(rid2, rid)
        self.assertEqual(action, "reinforced")
        row = self.conn.execute(
            "SELECT state, mention_count FROM remaining_items WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["state"], "reopened")
        self.assertEqual(row["mention_count"], 2)
        open_ids = {r["id"] for r in remaining.open_items(self.conn, work_item_id=self.w1)}
        self.assertIn(rid, open_ids)

    def test_resolved_item_is_never_reinforced_by_similarity_alone(self):
        a = "Retest upload recovery process thoroughly across every device configuration always"
        b = a + " urgently"
        # {retest, upload, recovery, process, thoroughly, across, every, device,
        #  configuration, always} (10) vs the same set + "urgently" (11)
        # -> intersection 10, union 11 -> 0.909..., comfortably above
        # REINFORCE_THRESHOLD, but different normalized keys.
        self.assertGreaterEqual(d.similarity(a, b), d.REINFORCE_THRESHOLD)
        self.assertNotEqual(d.normalize_key(a), d.normalize_key(b))

        rid, _ = d.resolve(self.conn, title=a, occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        remaining.transition(self.conn, rid, "completed", occurred_at="2026-08-29T12:00:00.000Z",
                              source="manual", confidence="high", explanation="done")
        rid2, action = d.resolve(self.conn, title=b, occurred_at="2026-08-30T12:00:00.000Z", **self.kw)
        self.assertEqual(action, "created")
        self.assertNotEqual(rid2, rid)
        row = self.conn.execute(
            "SELECT state, mention_count FROM remaining_items WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["mention_count"], 1)

    def test_silence_never_completes_an_item(self):
        rid, _ = d.resolve(self.conn, title="Retest upload", occurred_at="2026-08-28T12:00:00.000Z", **self.kw)
        d.resolve(self.conn, title="Retest upload", occurred_at="2026-08-29T12:00:00.000Z", **self.kw)
        self.assertEqual(self.conn.execute("SELECT state FROM remaining_items WHERE id=?", (rid,)).fetchone()[0], "open")
