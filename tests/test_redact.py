import unittest
from work_ledger import redact

CASES = {
    "bearer_token":        "Authorization: Bearer sk-abc123DEF456ghi789JKL012mno",
    "basic_auth":          "Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ1Ng==",
    "atlassian_token":     "token=ATATT3xFfGF0T4Jw9xQpLm2Zk8vRt6YbNc1Ud0Ha",
    "github_token":        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "slack_token":         "xoxb-123456789012-abcdefghijklmnop",
    "aws_access_key_id":   "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_key":      "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "api_key_assignment":  "api_key=abcd1234efgh5678",
    "password_assignment": "password=hunter2hunter2",
    "cookie_header":       "Cookie: session=9f8a7b6c5d4e3f2a1b0c",
    "jwt":                 "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "pem_private_key":     "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----",
    "url_credentials":     "https://user:s3cr3tpass@jira.example.com/rest",
    "env_secret_assignment": "JIRA_API_TOKEN=zzzzYYYYxxxxWWWW1234",
}

class TestRedact(unittest.TestCase):
    def test_every_pattern_fires_and_removes_the_literal(self):
        for name, sample in CASES.items():
            out, fired = redact.redact(sample)
            self.assertIn(name, fired, f"{name} did not fire on {sample!r}")
            self.assertIn("[REDACTED:", out)

    def test_secret_literal_never_survives(self):
        for name, sample in CASES.items():
            out, _ = redact.redact(sample)
            for literal in ("hunter2hunter2", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                            "ATATT3xFfGF0T4Jw9xQpLm2Zk8vRt6YbNc1Ud0Ha", "s3cr3tpass"):
                if literal in sample:
                    self.assertNotIn(literal, out)

    def test_credential_references_are_not_secrets(self):
        for ref in ("keychain://work-ledger/acme-jira", "env://JIRA_TOKEN_REF"):
            out, fired = redact.redact(ref)
            self.assertEqual(out, ref)
            self.assertEqual(fired, [])

    def test_redact_obj_uses_key_names(self):
        out, fired = redact.redact_obj({"headers": {"Authorization": "Bearer zzz"},
                                        "note": "nothing to see", "apiKey": "plainlookingvalue"})
        self.assertEqual(out["note"], "nothing to see")
        self.assertNotIn("plainlookingvalue", repr(out))
        self.assertNotIn("zzz", repr(out))

    def test_ordinary_prose_is_untouched(self):
        text = "Fixed the upload recovery path in AB-326 and ran the test suite."
        self.assertEqual(redact.redact(text), (text, []))

    def test_prose_mentioning_basic_or_bearer_is_not_mangled(self):
        # CRITICAL/IMPORTANT 3 repro: bare "Basic"/"Bearer" outside an
        # Authorization header context is ordinary prose, not a credential.
        for text in (
            "Basic auth is required for the legacy Jira REST endpoint.",
            "We should use a Bearer scheme for the new integration.",
        ):
            self.assertEqual(redact.redact(text), (text, []))

    def test_cookie_header_redacts_every_pair_not_just_the_first(self):
        # CRITICAL 1 repro: a real Set-Cookie/Cookie header has multiple
        # `;`-separated pairs; the old pattern stopped at the first space.
        for sample, secret in (
            ("Set-Cookie: theme=dark; sessionid=RAWSECRETSESSIONID12345; Path=/",
             "RAWSECRETSESSIONID12345"),
            ("Cookie: a=1; auth_token=SUPERSECRETVALUE9999; b=2",
             "SUPERSECRETVALUE9999"),
        ):
            out, fired = redact.redact(sample)
            self.assertIn("cookie_header", fired)
            self.assertNotIn(secret, out)

    def test_bare_uppercase_env_assignment_fires(self):
        # CRITICAL 2 repro: env_secret_assignment required a leading
        # identifier segment before the keyword, so a bare all-caps
        # assignment like PASSWORD=/TOKEN=/KEY=/SECRET= passed through.
        for sample, secret in (
            ("PASSWORD=SuperSecretPass123", "SuperSecretPass123"),
            ("TOKEN=zzzzYYYYxxxxWWWW1234", "zzzzYYYYxxxxWWWW1234"),
            ("KEY=abcdEFGH12345678", "abcdEFGH12345678"),
            ("SECRET=abcdEFGH12345678", "abcdEFGH12345678"),
            ("MY_API_KEY=abcdEFGH12345678", "abcdEFGH12345678"),
        ):
            out, fired = redact.redact(sample)
            self.assertIn("env_secret_assignment", fired, f"did not fire on {sample!r}")
            self.assertNotIn(secret, out)

    def test_env_secret_assignment_keeps_the_variable_name(self):
        # IMPORTANT 4: DESIGN §12 says "value replaced, name kept" for
        # environment assignments.
        out, fired = redact.redact("JIRA_API_TOKEN=zzzzYYYYxxxxWWWW1234")
        self.assertIn("env_secret_assignment", fired)
        self.assertTrue(out.startswith("JIRA_API_TOKEN="), out)
        self.assertNotIn("zzzzYYYYxxxxWWWW1234", out)

        out2, _ = redact.redact("PASSWORD=SuperSecretPass123")
        self.assertTrue(out2.startswith("PASSWORD="), out2)

    def test_redaction_is_idempotent(self):
        # Re-redacting already-redacted output must be a no-op, including
        # for the patterns touched by this fix (cookie_header, bearer_token,
        # basic_auth, env_secret_assignment).
        samples = list(CASES.values()) + [
            "Set-Cookie: theme=dark; sessionid=RAWSECRETSESSIONID12345; Path=/",
            "PASSWORD=SuperSecretPass123",
            "MY_API_KEY=abcdEFGH12345678",
        ]
        for sample in samples:
            once, _ = redact.redact(sample)
            twice, _ = redact.redact(once)
            self.assertEqual(once, twice, f"not idempotent for {sample!r}")
