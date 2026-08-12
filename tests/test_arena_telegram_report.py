import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import arena_telegram_report


class ArenaTelegramReportTests(unittest.TestCase):
    def test_missing_required_env_fails_closed_without_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sys, "argv", ["arena_telegram_report.py", "--env-file", str(Path(tmp) / "missing.env")]
        ), mock.patch.object(arena_telegram_report.subprocess, "run") as run:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = arena_telegram_report.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(payload["command"], "arena-telegram-report")
        self.assertEqual(payload["reason"], "missing_required_env")
        self.assertFalse(payload["safety"]["trading_mutations"])
        run.assert_not_called()

    def test_dry_run_loads_env_and_reports_inline_button_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=secret-token",
                        "FINAM_H4_TELEGRAM_CHAT_ID=-1001",
                        "FINAM_ARENA_API=arena-secret",
                    ]
                ),
                encoding="utf-8",
            )
            child_payload = {
                "status": "OK",
                "command": "arena-status",
                "telegram_delivery": "dry_run",
                "telegram_reply_markup": {
                    "inline_keyboard": [
                        [{"text": "overview", "callback_data": "arena:overview"}],
                        [
                            {"text": "ru", "callback_data": "arena:account:DEMO-RU"},
                            {"text": "us", "callback_data": "arena:account:DEMO-US"},
                        ],
                        [{"text": "ai", "callback_data": "arena:account:DEMO-AI"}],
                        [
                            {"text": "risks", "callback_data": "arena:risks"},
                            {"text": "strategy", "callback_data": "arena:strategy"},
                        ],
                        [{"text": "rotation", "callback_data": "arena:rotation"}],
                        [{"text": "attribution", "callback_data": "arena:attribution"}],
                    ]
                },
            }

            def fake_run(command, **kwargs):
                self.assertIn("arena-status", command)
                self.assertIn("--dry-run", command)
                self.assertNotIn("--send-telegram", command)
                self.assertEqual(kwargs["env"]["TELEGRAM_BOT_TOKEN"], "secret-token")
                return subprocess.CompletedProcess(command, 0, json.dumps(child_payload), "")

            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                sys, "argv", ["arena_telegram_report.py", "--env-file", str(env_file), "--dry-run"]
            ), mock.patch.object(arena_telegram_report.subprocess, "run", side_effect=fake_run):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = arena_telegram_report.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["wrapper_command"], "arena-telegram-report")
        self.assertEqual(payload["telegram_delivery"], "dry_run")
        self.assertEqual(payload["reply_markup_buttons"], 8)
        self.assertEqual(payload["env_files_loaded"], [str(env_file)])


if __name__ == "__main__":
    unittest.main()
