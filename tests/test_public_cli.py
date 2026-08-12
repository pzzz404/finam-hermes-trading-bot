import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot.__main__ import doctor, paper_demo  # noqa: E402


class PublicCliTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_doctor_is_offline_and_paper_by_default(self):
        result = doctor()
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["config"]["trading_mode"], "paper")
        self.assertFalse(result["network_calls"])
        self.assertFalse(result["broker_mutations"])

    @patch.dict(
        os.environ,
        {"TRADING_MODE": "live", "FINAM_TOKEN": "test-token", "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT"},
        clear=True,
    )
    def test_doctor_rejects_placeholder_account_in_live_mode(self):
        result = doctor()
        self.assertEqual(result["status"], "NO_TRADE")
        self.assertTrue(result["errors"])

    @patch.dict(os.environ, {}, clear=True)
    def test_paper_demo_is_deterministic_and_offline(self):
        result = paper_demo()
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["cash"], 99_000.0)
        self.assertEqual(result["positions"], {"DEMO": 10})
        self.assertFalse(result["network_calls"])
        self.assertFalse(result["broker_mutations"])

    def test_module_cli_outputs_json(self):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"), "TRADING_MODE": "paper"}
        run = subprocess.run(
            [sys.executable, "-m", "finam_trading_bot", "paper-demo", "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "OK")


if __name__ == "__main__":
    unittest.main()
