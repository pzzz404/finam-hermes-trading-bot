import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.snapshot import build_account_snapshot, extract_first_account_id, scrub


class FakeClient:
    def session_details(self, token):
        return {"account_ids": ["DEMO-ACCOUNT"], "token": "must-not-leak"}

    def get_account(self, token, account_id):
        return {"account_id": account_id, "secret": "must-not-leak"}

    def orders(self, token, account_id):
        return {"items": []}

    def trades(self, token, account_id, *, limit=None):
        return {"limit": limit, "items": []}

    def transactions(self, token, account_id, *, limit=None):
        return {"limit": limit, "items": []}


class SnapshotTests(unittest.TestCase):
    def test_scrub_redacts_sensitive_keys_recursively(self):
        value = {
            "token": "jwt",
            "nested": {"apiKey": "key", "items": [{"password": "pw", "safe": 1}]},
        }

        self.assertEqual(
            scrub(value),
            {
                "token": "[REDACTED]",
                "nested": {"apiKey": "[REDACTED]", "items": [{"password": "[REDACTED]", "safe": 1}]},
            },
        )

    def test_extract_first_account_id_supports_account_ids_shape(self):
        self.assertEqual(extract_first_account_id({"account_ids": ["DEMO-ACCOUNT"]}), "DEMO-ACCOUNT")

    def test_build_account_snapshot_calls_readonly_methods_and_scrubs_output(self):
        snapshot = build_account_snapshot(FakeClient(), "jwt", history_limit=10)

        self.assertEqual(snapshot["account_id"], "[REDACTED]")
        self.assertEqual(snapshot["session_details"]["token"], "[REDACTED]")
        self.assertEqual(snapshot["account"]["secret"], "[REDACTED]")
        self.assertEqual(snapshot["trades"]["limit"], 10)
        self.assertEqual(snapshot["transactions"]["limit"], 10)
        self.assertIn("orders", snapshot)

    def test_build_account_snapshot_accepts_explicit_account_id(self):
        class NoAccountListClient(FakeClient):
            def session_details(self, token):
                return {"readonly": False}

        snapshot = build_account_snapshot(
            NoAccountListClient(),
            "jwt",
            history_limit=10,
            account_id="DEMO-ACCOUNT",
        )

        self.assertEqual(snapshot["account_id"], "[REDACTED]")
        self.assertEqual(snapshot["account"]["account_id"], "[REDACTED]")

    def test_build_account_snapshot_keeps_partial_results_when_endpoint_fails(self):
        class PartiallyFailingClient(FakeClient):
            def trades(self, token, account_id, *, limit=None):
                raise RuntimeError("HTTP 400 while calling trades")

        snapshot = build_account_snapshot(PartiallyFailingClient(), "jwt", history_limit=10)

        self.assertEqual(snapshot["account_id"], "[REDACTED]")
        self.assertEqual(snapshot["trades"], {"error": "HTTP 400 while calling trades"})
        self.assertEqual(snapshot["transactions"]["limit"], 10)
        self.assertIn("orders", snapshot)


if __name__ == "__main__":
    unittest.main()
