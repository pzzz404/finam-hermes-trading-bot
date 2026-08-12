import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.config import finam_token_status, get_finam_account_id


class ConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_finam_token_status_missing(self):
        self.assertEqual(finam_token_status(), (False, 0))

    @patch.dict(os.environ, {"FINAM_TOKEN": "x" * 30})
    def test_finam_token_status_present(self):
        self.assertEqual(finam_token_status(), (True, 30))

    @patch.dict(os.environ, {"FINAM_ACCOUNT_ID": " DEMO-ACCOUNT "})
    def test_get_finam_account_id_strips_value(self):
        self.assertEqual(get_finam_account_id(), "DEMO-ACCOUNT")

    @patch.dict(os.environ, {"FINAM_ACCOUNT_ID": ""})
    def test_get_finam_account_id_empty(self):
        self.assertIsNone(get_finam_account_id())


if __name__ == "__main__":
    unittest.main()
