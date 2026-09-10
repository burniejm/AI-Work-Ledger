import re, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = {"README.md": ROOT / "README.md",
        "INSTALL": ROOT / "docs" / "INSTALL.md",
        "UPGRADE": ROOT / "docs" / "UPGRADE.md",
        "RECOVERY": ROOT / "docs" / "RECOVERY.md",
        "REMOVAL": ROOT / "docs" / "REMOVAL.md"}

class TestDocs(unittest.TestCase):
    def test_all_documents_exist_and_are_substantive(self):
        for name, p in DOCS.items():
            self.assertTrue(p.exists(), f"{name} missing")
            self.assertGreater(len(p.read_text()), 800, f"{name} is a stub")

    def test_readme_documents_every_command(self):
        text = DOCS["README.md"].read_text()
        for cmd in ("setup","today","day","range","status","remaining","blocked","stale","evidence",
                    "add","correct","inferences","connections","backup","export","import","doctor",
                    "uninstall"):
            self.assertIn(f"worklog {cmd}", text)

    def test_readme_states_the_non_goals_and_privacy_posture(self):
        low = DOCS["README.md"].read_text().lower()
        for phrase in ("read-only", "single-device", "no telemetry", "secrets"):
            self.assertIn(phrase, low)

    def test_install_warns_about_synced_filesystems(self):
        low = DOCS["INSTALL"].read_text().lower()
        for word in ("dropbox", "icloud"):
            self.assertIn(word, low)

    def test_recovery_covers_backup_reprocess_and_doctor(self):
        text = DOCS["RECOVERY"].read_text()
        for cmd in ("worklog doctor", "worklog restore", "worklog reprocess", "worklog connections repair"):
            self.assertIn(cmd, text)

    def test_removal_requires_explicit_credential_deletion(self):
        text = DOCS["REMOVAL"].read_text()
        self.assertIn("--delete-credentials", text)
        self.assertRegex(text.lower(), r"(explicit|confirm)")

    def test_removal_documents_the_real_uninstall_command(self):
        # Ruling 12: the command exists (Task 20); the docs must name it and its phrase.
        import sys
        sys.path.insert(0, str(ROOT / "src"))
        from work_ledger import uninstall
        text = DOCS["REMOVAL"].read_text()
        self.assertIn("worklog uninstall --data", text)
        self.assertIn(uninstall.CONFIRMATION_PHRASE, text)

    def test_readme_documents_uninstall(self):
        self.assertIn("worklog uninstall", DOCS["README.md"].read_text())

    def test_no_stale_install_notes_file(self):
        self.assertFalse((ROOT / "docs" / "INSTALL-NOTES.md").exists())

    def test_docs_reference_the_known_gaps(self):
        self.assertIn("DESIGN.md", DOCS["README.md"].read_text())
