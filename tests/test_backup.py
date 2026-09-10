import json, os, sqlite3, tempfile, unittest
from pathlib import Path
from work_ledger.db import connection as dbc, migrations as m
from work_ledger import backup, corrections, entities as e, work_items as wi, ids, timeutil

class TestBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
        self.path = os.path.join(self.tmp.name, "work-ledger.sqlite")
        self.conn = dbc.connect(self.path)
        m.apply_all(self.conn)
        self.cl = e.upsert_client(self.conn, "Acme")
        self.wi = wi.upsert_external(self.conn, "c1", "AB-76", title="Upload recovery", client_id=self.cl)
        now = timeutil.utc_now_iso()
        self.conn.execute("INSERT INTO connections(id,name,adapter,credential_reference,origin_device_id,"
                          "created_at,updated_at) VALUES ('c1','acme-jira','jira-rest',"
                          "'keychain://work-ledger/acme-jira','d',?,?)", (now, now))
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_backup_is_a_valid_readable_database(self):
        p = backup.create_backup(self.conn)
        self.assertTrue(p.exists())
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        c2 = sqlite3.connect(p)
        self.assertEqual(c2.execute("SELECT count(*) FROM work_items").fetchone()[0], 1)
        self.assertEqual(c2.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_a_label_containing_a_slash_is_rejected(self):
        # Minor #6 (final review): a '/' in label would otherwise let the
        # backup escape paths.backup_dir() (or fail with an opaque sqlite
        # "unable to open database file").
        with self.assertRaises(ValueError):
            backup.create_backup(self.conn, label="pre/post migration")
        backup_dir = Path(self.tmp.name) / "backups"
        if backup_dir.is_dir():
            self.assertEqual(list(backup_dir.iterdir()), [])

    def test_an_ordinary_label_is_accepted_and_appears_in_the_filename(self):
        p = backup.create_backup(self.conn, label="pre-restore")
        self.assertIn("pre-restore", p.name)

    def test_backup_contains_no_credential_reference_value_after_export(self):
        out = Path(self.tmp.name) / "export.json"
        backup.export_json(self.conn, out)
        data = json.loads(out.read_text())
        conns = data["tables"]["connections"]
        self.assertIsNone(conns[0]["credential_reference"])
        self.assertNotIn("keychain://work-ledger/acme-jira", out.read_text())

    def test_round_trip_reproduces_structured_history(self):
        out = Path(self.tmp.name) / "export.json"
        backup.export_json(self.conn, out)
        target = os.path.join(self.tmp.name, "restored.sqlite")
        c2 = dbc.connect(target); m.apply_all(c2)
        stats = backup.import_json(c2, out)
        self.assertGreaterEqual(stats["inserted"], 2)
        self.assertEqual(c2.execute("SELECT external_key FROM work_items").fetchone()[0], "AB-76")

    def test_import_is_idempotent(self):
        out = Path(self.tmp.name) / "export.json"
        backup.export_json(self.conn, out)
        target = os.path.join(self.tmp.name, "restored.sqlite")
        c2 = dbc.connect(target); m.apply_all(c2)
        backup.import_json(c2, out)
        before = c2.execute("SELECT count(*) FROM work_items").fetchone()[0]
        second = backup.import_json(c2, out)
        self.assertEqual(c2.execute("SELECT count(*) FROM work_items").fetchone()[0], before)
        self.assertEqual(second["inserted"], 0)

    def test_export_covers_every_table_with_its_real_primary_key(self):
        # Ruling 16: three tables have no `id` column; the export list names their keys.
        keys = dict(backup.EXPORT_TABLES)
        self.assertEqual(keys["settings"], "key")
        self.assertEqual(keys["schema_migrations"], "version")
        self.assertEqual(keys["display_id_counters"], "scope")
        live = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(live, set(keys))
        for table, pk in backup.EXPORT_TABLES:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            self.assertIn(pk, cols, f"{table} has no column {pk}")

    def test_import_order_is_topological_and_total(self):
        # Ruling 17: parents strictly precede children, and every exported table is ordered.
        self.assertEqual(set(backup.IMPORT_ORDER), {t for t, _ in backup.EXPORT_TABLES})
        pos = {t: i for i, t in enumerate(backup.IMPORT_ORDER)}
        for child in backup.IMPORT_ORDER:
            for fk in self.conn.execute(f"PRAGMA foreign_key_list({child})"):
                parent = fk["table"]
                if parent == child:
                    continue          # self-reference: resolved by the deferred second pass
                self.assertLess(pos[parent], pos[child], f"{parent} must be imported before {child}")

    def test_import_into_a_fresh_db_respects_foreign_keys(self):
        # Ruling 17: FKs stay ON; ordering alone must make the import legal.
        out = Path(self.tmp.name) / "export.json"
        backup.export_json(self.conn, out)
        target = os.path.join(self.tmp.name, "fk.sqlite")
        c2 = dbc.connect(target); m.apply_all(c2)
        self.assertEqual(c2.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        backup.import_json(c2, out)
        self.assertEqual(c2.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_markdown_and_csv_exports_are_produced(self):
        md = Path(self.tmp.name) / "history.md"
        backup.export_markdown(self.conn, md, tz="America/Chicago", rollover_hour=3)
        self.assertIn("Acme", md.read_text())
        files = backup.export_csv(self.conn, Path(self.tmp.name) / "csv")
        self.assertTrue(any(f.name == "activities.csv" for f in files))

    # --- Review fixes -----------------------------------------------------

    def test_stale_work_item_import_never_applies_deferred_self_reference(self):
        # Critical: a work_items row skipped as stale (existing updated_at is
        # newer than the import's) must never have its deferred
        # merged_into_id update applied -- that update belongs only to rows
        # actually inserted or updated this pass.
        other_wi = wi.upsert_external(self.conn, "c1", "AB-77", title="Other ticket", client_id=self.cl)

        out = Path(self.tmp.name) / "export.json"
        data = backup.export_json(self.conn, out)

        target = os.path.join(self.tmp.name, "stale_merge.sqlite")
        c2 = dbc.connect(target); m.apply_all(c2)
        backup.import_json(c2, out)  # fresh insert of both work items, merged_into_id NULL

        # Make the live AB-76 row look newer than anything a later import offers.
        c2.execute("UPDATE work_items SET updated_at=? WHERE id=?",
                   ("2099-01-01T00:00:00.000Z", self.wi))
        self.assertIsNone(
            c2.execute("SELECT merged_into_id FROM work_items WHERE id=?", (self.wi,)).fetchone()["merged_into_id"]
        )

        # Craft a "stale" re-import: same (older) updated_at as the original
        # export, but now claiming AB-76 was merged into AB-77.
        stale_row = next(r for r in data["tables"]["work_items"] if r["id"] == self.wi)
        stale_row["merged_into_id"] = other_wi
        stale_path = Path(self.tmp.name) / "stale_export.json"
        stale_path.write_text(json.dumps(data))

        stats = backup.import_json(c2, stale_path)
        self.assertEqual(stats["updated"], 0)
        row = c2.execute("SELECT merged_into_id FROM work_items WHERE id=?", (self.wi,)).fetchone()
        self.assertIsNone(row["merged_into_id"],
                          "a stale-skipped import must not apply the deferred self-reference update")

    def test_reimported_corrections_never_receive_deferred_updates(self):
        # Critical: corrections is append-only. A row already present (the
        # common re-import case) must be skipped outright -- including never
        # having its deferred undo_of_correction_id update applied.
        corrections.apply_field_update(self.conn, "clients", self.cl, "notes", "note", reason="r1")
        group_id = self.conn.execute(
            "SELECT correction_group_id FROM corrections ORDER BY applied_at LIMIT 1"
        ).fetchone()["correction_group_id"]
        corrections.undo(self.conn, group_id)

        out = Path(self.tmp.name) / "export.json"
        data = backup.export_json(self.conn, out)

        target = os.path.join(self.tmp.name, "corrections.sqlite")
        c2 = dbc.connect(target); m.apply_all(c2)
        backup.import_json(c2, out)

        before = {r["id"]: r["undo_of_correction_id"]
                  for r in c2.execute("SELECT id, undo_of_correction_id FROM corrections")}
        before_count = c2.execute("SELECT count(*) FROM corrections").fetchone()[0]
        self.assertTrue(any(v is not None for v in before.values()), "fixture must include an undo row")

        # Corrupt the export's undo pointers before re-importing, simulating
        # a stale/duplicate re-import that must have zero effect.
        for row in data["tables"]["corrections"]:
            if row["undo_of_correction_id"]:
                row["undo_of_correction_id"] = "not-a-real-id"
        tampered = Path(self.tmp.name) / "tampered_export.json"
        tampered.write_text(json.dumps(data))

        stats = backup.import_json(c2, tampered)
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["updated"], 0)
        after = {r["id"]: r["undo_of_correction_id"]
                 for r in c2.execute("SELECT id, undo_of_correction_id FROM corrections")}
        self.assertEqual(after, before)
        self.assertEqual(c2.execute("SELECT count(*) FROM corrections").fetchone()[0], before_count)

    def test_settings_device_key_round_trips_and_device_identity_is_stable(self):
        # Important A: device_key must not be treated as credential-related --
        # dropping it forks device identity, since current_device() mints a
        # fresh one and orphans the imported devices row.
        original_device_id = self.conn.execute("SELECT id FROM devices LIMIT 1").fetchone()["id"]

        out = Path(self.tmp.name) / "export.json"
        data = backup.export_json(self.conn, out)
        settings_keys = {row["key"] for row in data["tables"]["settings"]}
        self.assertIn("device_key", settings_keys)

        target = os.path.join(self.tmp.name, "device.sqlite")
        c2 = dbc.connect(target); m.apply_all(c2)
        backup.import_json(c2, out)

        device_id = e.current_device(c2)
        self.assertEqual(device_id, original_device_id)
        self.assertEqual(c2.execute("SELECT count(*) FROM devices").fetchone()[0], 1)

    def test_import_update_preserves_live_credential_reference(self):
        # Important B: the UPDATE path must never write EXPORT_EXCLUDED_COLUMNS
        # -- a live credential_reference re-established since the export was
        # taken must survive a newer-updated_at import of that same row.
        out = Path(self.tmp.name) / "export.json"
        backup.export_json(self.conn, out)

        target = os.path.join(self.tmp.name, "creds.sqlite")
        c2 = dbc.connect(target); m.apply_all(c2)
        old = "2000-01-01T00:00:00.000Z"
        c2.execute(
            "INSERT INTO connections(id,name,adapter,credential_reference,origin_device_id,"
            "created_at,updated_at) VALUES ('c1','acme-jira','jira-rest',"
            "'keychain://work-ledger/real-secret','d',?,?)", (old, old),
        )

        stats = backup.import_json(c2, out)
        self.assertGreater(stats["updated"], 0)
        row = c2.execute(
            "SELECT credential_reference FROM connections WHERE id='c1'"
        ).fetchone()
        self.assertEqual(row["credential_reference"], "keychain://work-ledger/real-secret")
