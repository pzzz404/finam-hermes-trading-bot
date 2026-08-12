import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.operator_journal import write_event  # noqa: E402


class OperatorJournalTests(unittest.TestCase):
    def test_write_event_scrubs_secrets_and_preserves_safety_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            write_event(
                {"command": "scan", "api_key": "secret", "account_id": "private", "status": "OK"},
                path=path,
            )

            item = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(item["command"], "scan")
        self.assertEqual(item["api_key"], "[REDACTED]")
        self.assertEqual(item["account_id"], "[REDACTED]")
        self.assertTrue(item["read_only"])
        self.assertFalse(item["trading_mutations"])
        self.assertFalse(item["production_send"])


if __name__ == "__main__":
    unittest.main()
