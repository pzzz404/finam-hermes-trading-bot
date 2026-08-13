import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.redaction import redact_environment_values, redact_text, scrub  # noqa: E402


class RedactionTests(unittest.TestCase):
    def test_scrub_redacts_identifiers_and_credentials(self):
        value = {
            "account_id": "REAL",
            "telegram_chat_id": "123",
            "nested": {"session_id": "abc", "safe": "ok"},
        }
        self.assertEqual(
            scrub(value),
            {
                "account_id": "[REDACTED]",
                "telegram_chat_id": "[REDACTED]",
                "nested": {"session_id": "[REDACTED]", "safe": "ok"},
            },
        )

    def test_redact_text_masks_bearer_and_assignments(self):
        text = redact_text("Authorization: Bearer abc.def token=top-secret")
        self.assertNotIn("abc.def", text)
        self.assertNotIn("top-secret", text)

    def test_redact_text_masks_identifiers_and_telegram_bot_urls(self):
        text = redact_text(
            "Account with id REAL-123 is missing; chat_id=-100123; "
            "https://api.telegram.org/bot123456:secret/getMe"
        )
        self.assertNotIn("REAL-123", text)
        self.assertNotIn("-100123", text)
        self.assertNotIn("123456:secret", text)

    def test_redact_environment_values_masks_nested_exception_text(self):
        sentinel = "sentinel-secret-value"
        value = {
            "errors": [f"request failed: Bearer {sentinel}"],
            "nested": {"detail": RuntimeError(f"endpoint={sentinel}")},
        }

        redacted = redact_environment_values(value, env={"FINAM_ARENA_API": sentinel})

        self.assertNotIn(sentinel, str(redacted))
        self.assertIn("[REDACTED]", str(redacted))


if __name__ == "__main__":
    unittest.main()
