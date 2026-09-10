import copy, os, sys, tempfile, unittest
from unittest import mock
from work_ledger import config, paths, yamlite

class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name
    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def test_creates_defaults_on_first_load(self):
        cfg = config.load()
        self.assertEqual(cfg["timezone"], "America/Chicago")
        self.assertEqual(cfg["rollover_hour"], 3)
        self.assertTrue(paths.config_path().exists())
        self.assertEqual(oct(paths.config_path().stat().st_mode & 0o777), "0o600")

    def test_partial_file_merges_over_defaults(self):
        paths.ensure_dirs()
        paths.config_path().write_text("timezone: UTC\nstage_b:\n  model: haiku\n")
        cfg = config.load()
        self.assertEqual(cfg["timezone"], "UTC")
        self.assertEqual(cfg["stage_b"]["model"], "haiku")
        self.assertEqual(cfg["stage_b"]["timeout_seconds"], 120)  # default survives

    def test_dotted_get(self):
        config.load()
        self.assertEqual(config.get("jira.stale_after_hours"), 12)

    def test_validate_flags_bad_timezone_and_rollover(self):
        problems = config.validate({**config.DEFAULTS, "timezone": "Mars/Olympus", "rollover_hour": 99})
        self.assertEqual(len(problems), 2)

    def test_validate_flags_secret_in_config(self):
        # Ruling 29: DEFAULTS carries no `connections` list, so the secret sweep is
        # exercised through an ordinary scalar value instead.
        cfg = {**config.DEFAULTS,
               "device_name": "ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz012345"}
        self.assertTrue(any("secret" in p.lower() for p in config.validate(cfg)))

    def test_validate_accepts_credential_references(self):
        cfg = {**config.DEFAULTS, "device_name": "keychain://work-ledger/acme-jira"}
        self.assertEqual(config.validate(cfg), [])

    def test_defaults_carry_no_connections_or_repositories(self):
        # Ruling 29: the DB tables are authoritative; config must not shadow them.
        self.assertNotIn("connections", config.DEFAULTS)
        self.assertNotIn("repositories", config.DEFAULTS)
        self.assertNotIn("enabled", config.DEFAULTS["jira"])

    def test_validate_flags_non_integer_payload_max_bytes(self):
        cfg = {**config.DEFAULTS,
               "capture": {**config.DEFAULTS["capture"], "payload_max_bytes": "8192"}}
        problems = config.validate(cfg)
        self.assertTrue(any("payload_max_bytes" in p for p in problems))

    def test_validate_flags_non_integer_excerpt_max_chars(self):
        cfg = {**config.DEFAULTS,
               "capture": {**config.DEFAULTS["capture"], "excerpt_max_chars": None}}
        problems = config.validate(cfg)
        self.assertTrue(any("excerpt_max_chars" in p for p in problems))

    def test_validate_flags_non_list_noise_tools(self):
        cfg = {**config.DEFAULTS,
               "capture": {**config.DEFAULTS["capture"], "noise_tools": "Read,Glob"}}
        problems = config.validate(cfg)
        self.assertTrue(any("noise_tools" in p for p in problems))

    def test_config_delegates_secret_detection_to_redact(self):
        import inspect, re as _re
        from work_ledger import config as cfgmod, redact
        src = inspect.getsource(cfgmod)
        self.assertIn("looks_like_secret", src)
        self.assertNotIn("TEMPORARY", src)               # the placeholder regex is gone
        for token in ("ATATT", "ghp_", "xox", "AKIA", "eyJ"):
            self.assertNotIn(token, src, "config.py must not carry credential patterns")
        self.assertTrue(redact.looks_like_secret("ATATT3xFfGF0abcdefghijklmnopqrstuvwxyz012345"))
        self.assertFalse(redact.looks_like_secret("keychain://work-ledger/acme-jira"))


class TestConfigLoaderOrdering(unittest.TestCase):
    """CONTROLLER RULING: yamlite loads first, always; PyYAML is only a
    fallback for text outside the yamlite subset. See the config.py module
    docstring for why the order can't be the other way around."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["WORK_LEDGER_HOME"] = self.tmp.name

    def tearDown(self):
        del os.environ["WORK_LEDGER_HOME"]; self.tmp.cleanup()

    def _without_pyyaml(self):
        # sys.modules[name] = None forces `import yaml` to raise ImportError
        # even though PyYAML is actually installed, so a test can prove
        # behavior holds identically with and without it.
        return mock.patch.dict(sys.modules, {"yaml": None})

    def test_hand_edited_yaml_uses_yamlite_semantics_regardless_of_pyyaml(self):
        # The documented divergence fixture: PyYAML resolves YAML 1.1 idioms
        # (no -> False, 010 -> octal 8, 08 -> the string "08") that yamlite
        # deliberately does not (no -> "no", 010 -> int 10, 08 -> int 8).
        text = "stage_b:\n  enabled: no\nrollover_hour: 010\nnominal_start_hour: 08\n"
        paths.ensure_dirs()
        paths.config_path().write_text(text)

        cfg_with_pyyaml = config.load()
        with self._without_pyyaml():
            cfg_without_pyyaml = config.load()

        for cfg in (cfg_with_pyyaml, cfg_without_pyyaml):
            self.assertEqual(cfg["stage_b"]["enabled"], "no")
            self.assertEqual(cfg["rollover_hour"], 10)
            self.assertEqual(cfg["nominal_start_hour"], 8)

    def test_falls_back_to_pyyaml_for_syntax_outside_the_yamlite_subset(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        paths.ensure_dirs()
        paths.config_path().write_text("tags: [a, b, c]\n")  # flow collection: rejected by yamlite
        cfg = config.load()
        self.assertEqual(cfg["tags"], ["a", "b", "c"])

    def test_loaders_agree_on_a_saved_config(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["device_name"] = "keychain://work-ledger/acme-jira"
        config.save(cfg)
        text = paths.config_path().read_text()
        self.assertEqual(yamlite.loads(text), yaml.safe_load(text))
