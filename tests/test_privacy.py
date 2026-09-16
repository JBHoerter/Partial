"""Redaction fixpoint tests for ``partial.privacy``.

``redact()`` rewrites sensitive assignments to ``KEY = [REDACTED]``.
``has_secret_pattern()`` must treat the ``[REDACTED]`` placeholder as
non-secret so documents/bundles produced by redaction itself do not
fail import validation as "unsanitized secrets" (which can wedge
``sync --pull``), while real assignments and every other token pattern
remain detected.
"""

import json
import unittest

from partial.privacy import (
    REDACTED, has_secret_pattern, is_sensitive_path, redact)


class TestAssignPlaceholder(unittest.TestCase):
    """The [REDACTED] placeholder is not itself a secret."""

    def test_bare_placeholder_not_flagged(self):
        for text in (
                f"password = {REDACTED}",
                f"PASSWORD={REDACTED}",
                f"api_key: {REDACTED}",
                f"token={REDACTED};",
                f"secret = {REDACTED} and nothing else",
                f"line one\nsecret = {REDACTED}\nline three",
        ):
            with self.subTest(text=text):
                self.assertFalse(has_secret_pattern(text))

    def test_quoted_placeholder_not_flagged(self):
        for text in (
                f'password = "{REDACTED}"',
                f"token: '{REDACTED}'",
        ):
            with self.subTest(text=text):
                self.assertFalse(has_secret_pattern(text))

    def test_placeholder_inside_larger_value_still_flagged(self):
        # Only a value that is exactly the placeholder is exempt.
        for text in (
                f"password = {REDACTED}extra",
                f"password = x{REDACTED}",
                f'password = "x{REDACTED}"',
                f"password = '{REDACTED} tail'",
        ):
            with self.subTest(text=text):
                self.assertTrue(has_secret_pattern(text))


class TestRealAssignmentsStillDetected(unittest.TestCase):
    def test_assignment_forms_flagged(self):
        for text in (
                "password = hunter2",
                "PASSWORD=hunter2",
                "api_key: abcdef123456",
                "API-KEY = abcdef123456",
                "secret = \"top secret\"",
                "token = 'abc123'",
                "access_token:e5f6a7b8",
                "authorization: basic qvxv",
                "auth = letmein",
        ):
            with self.subTest(text=text):
                self.assertTrue(has_secret_pattern(text))

    def test_token_patterns_flagged(self):
        pem = ("-----BEGIN PRIVATE KEY-----\n"
               "abc123\n"
               "-----END PRIVATE KEY-----")
        for text in (
                "Bearer abcdef123456",
                "header: sk-abc123XYZ",
                "ghp_" + "a" * 20,
                "github_pat_" + "A1_" * 8,
                "AKIA" + "A" * 16,
                pem,
        ):
            with self.subTest(text=text):
                self.assertTrue(has_secret_pattern(text))

    def test_non_string_input_not_flagged(self):
        self.assertFalse(has_secret_pattern(None))
        self.assertFalse(has_secret_pattern(123))
        self.assertFalse(has_secret_pattern(["password = x"]))


class TestRedactFixpoint(unittest.TestCase):
    """redact() output is stable and passes the secret scan."""

    SAMPLE = ("password = hunter2\n"
              "api_key: \"abcdef123456\"\n"
              "Authorization: Bearer abcdef123456\n"
              "key: sk-abc123XYZ\n"
              "ghp_" + "a" * 20 + "\n"
              "-----BEGIN PRIVATE KEY-----\nx\n"
              "-----END PRIVATE KEY-----\n")

    def test_redact_output_passes_scan(self):
        out = redact(self.SAMPLE)
        self.assertIsInstance(out, str)
        self.assertIn(REDACTED, out)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("abcdef123456", out)
        self.assertFalse(has_secret_pattern(out))

    def test_repeated_redaction_is_stable(self):
        once = redact(self.SAMPLE)
        twice = redact(once)
        self.assertEqual(once, twice)
        self.assertFalse(has_secret_pattern(twice))

    def test_placeholder_text_unchanged_by_redact(self):
        text = (f"password = {REDACTED}\n"
                f"token: '{REDACTED}'\n")
        self.assertEqual(redact(text), text)

    def test_redacted_dict_serialized_passes_scan(self):
        blob = json.dumps(redact({
            "password": "hunter2",
            "note": "token = abc123",
            "nested": {"api_key": "zzz", "ok": "fine"},
        }))
        self.assertFalse(has_secret_pattern(blob))

    def test_mixed_placeholder_and_real_secret_flagged(self):
        text = (f"password = {REDACTED}\n"
                "api_key = stillrealsecret\n")
        self.assertTrue(has_secret_pattern(text))
        self.assertFalse(has_secret_pattern(redact(text)))


class TestSensitivePath(unittest.TestCase):
    def test_basic_sensitive_paths(self):
        for path in (".env", ".env.local", "credentials.json",
                     "a/b/.git/config", "keys/id_rsa.pem",
                     "cert/server.key"):
            with self.subTest(path=path):
                self.assertTrue(is_sensitive_path(path))
        for path in ("src/main.py", "docs/env.md", ""):
            with self.subTest(path=path):
                self.assertFalse(is_sensitive_path(path))


if __name__ == "__main__":
    unittest.main()
