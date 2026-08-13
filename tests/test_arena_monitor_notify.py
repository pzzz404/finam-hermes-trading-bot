import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import arena_monitor_notify


class ArenaMonitorNotifyTests(unittest.TestCase):
    def test_cli_redacts_environment_secret_from_output(self):
        sentinel = "arena-monitor-sentinel-secret"
        output = {"status": "FAILED", "telegram_delivery": "failed", "errors": [f"request failed: {sentinel}"]}

        with mock.patch.object(arena_monitor_notify.hermes_operator, "_load_default_operator_env"), mock.patch.object(
            arena_monitor_notify, "build_arena_pulse_output", return_value=output
        ), mock.patch.dict(os.environ, {"FINAM_TOKEN": sentinel}, clear=False), mock.patch.object(
            sys, "argv", ["arena_monitor_notify.py", "--once", "--dry-run"]
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = arena_monitor_notify.main()

        self.assertEqual(result, 1)
        self.assertNotIn(sentinel, stdout.getvalue())
        self.assertIn("[REDACTED]", stdout.getvalue())

    def test_dry_run_builds_pulse_without_sending(self):
        report = {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "label": "РФ", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-US", "label": "США", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-AI", "label": "AI", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
            ],
            "candidates": [{"symbol": "SBER@MISX"}],
            "errors": [],
            "warnings": [],
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_monitor_notify, "load_arena_policy", return_value={"mode": "approval"}
        ), mock.patch.object(
            arena_monitor_notify.hermes_operator,
            "_arena_status_output",
            return_value=report
            | {
                "telegram_pulse": arena_monitor_notify.h4_monitor_notify.format_arena_pulse(report),
                "telegram_reply_markup": arena_monitor_notify.h4_monitor_notify.arena_reply_markup(),
            },
        ), mock.patch.object(
            arena_monitor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_monitor_notify.build_arena_pulse_output(Path(tmp) / "policy.json", dry_run=True)

        self.assertEqual(output["command"], "arena-pulse")
        self.assertEqual(output["telegram_delivery"], "dry_run")
        self.assertEqual(output["source_command"], "arena-status")
        self.assertEqual(output["accounts_count"], 3)
        self.assertGreater(output["telegram_text_chars"], 0)
        self.assertIn("full_detail_command", output)
        self.assertFalse(output["safety"]["trading_mutations"])
        send.assert_not_called()

    def test_dry_run_full_json_keeps_payload(self):
        report = {"status": "OK", "mode": "approval", "accounts": [], "errors": [], "warnings": []}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_monitor_notify, "load_arena_policy", return_value={"mode": "approval"}
        ), mock.patch.object(
            arena_monitor_notify.hermes_operator,
            "_arena_status_output",
            return_value=report
            | {
                "telegram_pulse": arena_monitor_notify.h4_monitor_notify.format_arena_pulse(report),
                "telegram_reply_markup": arena_monitor_notify.h4_monitor_notify.arena_reply_markup(),
            },
        ):
            output = arena_monitor_notify.build_arena_pulse_output(Path(tmp) / "policy.json", dry_run=True, full_json=True)

        self.assertIn("telegram_text", output)
        self.assertIn("telegram_reply_markup", output)

    def test_send_mode_uses_inline_markup(self):
        report = {"status": "OK", "mode": "approval", "accounts": [], "candidates": [], "errors": [], "warnings": []}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_monitor_notify, "load_arena_policy", return_value={"mode": "approval"}
        ), mock.patch.object(
            arena_monitor_notify.hermes_operator,
            "_arena_status_output",
            return_value=report
            | {
                "telegram_pulse": arena_monitor_notify.h4_monitor_notify.format_arena_pulse(report),
                "telegram_reply_markup": arena_monitor_notify.h4_monitor_notify.arena_reply_markup(),
            },
        ), mock.patch.object(
            arena_monitor_notify.h4_monitor_notify, "send_telegram_message"
        ) as send:
            output = arena_monitor_notify.build_arena_pulse_output(Path(tmp) / "policy.json", dry_run=False)

        self.assertEqual(output["telegram_delivery"], "ok")
        send.assert_called_once()
        self.assertIn("reply_markup", send.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
