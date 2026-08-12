import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

spec = importlib.util.spec_from_file_location("arena_telegram_callbacks_script", ROOT / "scripts" / "arena_telegram_callbacks.py")
arena_telegram_callbacks = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(arena_telegram_callbacks)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ArenaTelegramCallbacksTests(unittest.TestCase):
    def sample_scan(self):
        return {
            "status": "OK",
            "mode": "approval",
            "accounts": [
                {"account_id": "DEMO-RU", "label": "РФ", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-US", "label": "США", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
                {"account_id": "DEMO-AI", "label": "AI", "equity": "1000000", "pnl_rub": "0", "pnl_pct": "0", "positions_count": 0},
            ],
            "candidates": [],
            "errors": [],
            "warnings": [],
        }

    def telegram_updates(self):
        return {
            "ok": True,
            "result": [
                {
                    "update_id": 123,
                    "callback_query": {
                        "id": "cb-1",
                        "data": "arena:account:DEMO-RU",
                        "message": {"message_id": 456, "chat": {"id": -100123}},
                    },
                }
            ],
        }

    def telegram_strategy_updates(self):
        return {
            "ok": True,
            "result": [
                {
                    "update_id": 321,
                    "callback_query": {
                        "id": "cb-2",
                        "data": "arena:st:DEMO-RU:auto",
                        "message": {"message_id": 654, "chat": {"id": -100123}},
                    },
                }
            ],
        }

    def test_dry_run_processes_arena_callback_without_write_apis(self):
        opener = mock.Mock(return_value=FakeResponse(self.telegram_updates()))

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_telegram_callbacks, "load_arena_policy", return_value={"mode": "approval", "accounts": []}
        ), mock.patch.object(
            arena_telegram_callbacks.hermes_operator, "build_arena_scan", return_value=self.sample_scan()
        ):
            offset = Path(tmp) / "offset.json"
            output = arena_telegram_callbacks.process_callbacks(
                policy_path=Path(tmp) / "policy.json",
                offset_state=offset,
                dry_run=True,
                env={"TELEGRAM_BOT_TOKEN": "secret-token", "FINAM_ARENA_CALLBACK_POLLING_ENABLED": "true"},
                opener=opener,
            )
            offset_payload = json.loads(offset.read_text(encoding="utf-8"))

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["updates_count"], 1)
        self.assertEqual(output["actions"][0]["callback_data"], "arena:account:DEMO-RU")
        self.assertEqual(output["actions"][0]["telegram_delivery"], "dry_run")
        self.assertEqual(offset_payload["offset"], 124)
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn("secret-token", json.dumps(output, ensure_ascii=False))

    def test_dry_run_processes_strategy_proposal_callback_without_policy_write(self):
        opener = mock.Mock(return_value=FakeResponse(self.telegram_strategy_updates()))
        policy = {
            "mode": "approval",
            "accounts": [
                {
                    "account_id": "DEMO-RU",
                    "label": "РФ",
                    "markets": ["MISX"],
                    "universe": ["SBER@MISX"],
                    "trade_mode": "manual",
                    "risk_multiplier": "1.0",
                },
                {"account_id": "DEMO-US", "markets": ["XNGS"], "universe": ["AAPL@XNGS"]},
                {"account_id": "DEMO-AI", "markets": ["MISX"], "universe": ["MOEX@MISX"]},
            ],
            "risk": {
                "risk_per_trade_pct": "1",
                "max_position_notional_pct": "25",
                "max_daily_loss_pct": "3",
                "max_account_drawdown_pct": "8",
                "max_open_risk_pct": "5",
                "max_open_positions": 8,
                "max_new_trades_per_account_per_run": 1,
            },
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_telegram_callbacks, "load_arena_policy", return_value=policy
        ):
            offset = Path(tmp) / "offset.json"
            output = arena_telegram_callbacks.process_callbacks(
                policy_path=Path(tmp) / "policy.json",
                offset_state=offset,
                dry_run=True,
                env={"TELEGRAM_BOT_TOKEN": "secret-token", "FINAM_ARENA_CALLBACK_POLLING_ENABLED": "true"},
                opener=opener,
            )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["actions"][0]["callback_data"], "arena:st:DEMO-RU:auto")
        self.assertEqual(output["actions"][0]["telegram_delivery"], "dry_run")
        self.assertFalse(output["safety"]["policy_write"])
        self.assertEqual(opener.call_count, 1)

    def test_callback_answers_and_edits_message(self):
        opener = mock.Mock(
            side_effect=[
                FakeResponse(self.telegram_updates()),
                FakeResponse({"ok": True, "result": True}),
                FakeResponse({"ok": True, "result": {"message_id": 456}}),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            arena_telegram_callbacks, "load_arena_policy", return_value={"mode": "approval", "accounts": []}
        ), mock.patch.object(
            arena_telegram_callbacks.hermes_operator, "build_arena_scan", return_value=self.sample_scan()
        ):
            output = arena_telegram_callbacks.process_callbacks(
                policy_path=Path(tmp) / "policy.json",
                offset_state=Path(tmp) / "offset.json",
                dry_run=False,
                env={"TELEGRAM_BOT_TOKEN": "secret-token", "FINAM_ARENA_CALLBACK_POLLING_ENABLED": "true"},
                opener=opener,
            )

        self.assertEqual(output["status"], "OK")
        self.assertEqual(output["actions"][0]["telegram_delivery"], "ok")
        urls = [call.args[0].full_url for call in opener.call_args_list]
        self.assertTrue(urls[0].endswith("/getUpdates"))
        self.assertTrue(urls[1].endswith("/answerCallbackQuery"))
        self.assertTrue(urls[2].endswith("/editMessageText"))
        edit_payload = arena_telegram_callbacks.urllib.parse.parse_qs(opener.call_args_list[2].args[0].data.decode("utf-8"))
        self.assertEqual(edit_payload["chat_id"][0], "-100123")
        self.assertEqual(edit_payload["message_id"][0], "456")
        self.assertEqual(edit_payload["parse_mode"][0], "HTML")
        self.assertIn("arena:overview", edit_payload["reply_markup"][0])

    def test_missing_token_is_no_data(self):
        output = arena_telegram_callbacks.process_callbacks(
            policy_path=Path("config/finam_arena_policy.json"),
            offset_state=Path("data/runtime/test-offset.json"),
            dry_run=False,
            env={},
            opener=mock.Mock(),
        )

        self.assertEqual(output["status"], "NO_DATA")
        self.assertEqual(output["reason"], "TELEGRAM_BOT_TOKEN_not_set")
        self.assertFalse(output["safety"]["trading_mutations"])

    def test_polling_requires_explicit_enable_flag(self):
        opener = mock.Mock(return_value=FakeResponse(self.telegram_updates()))

        output = arena_telegram_callbacks.process_callbacks(
            policy_path=Path("config/finam_arena_policy.json"),
            offset_state=Path("data/runtime/test-offset.json"),
            dry_run=False,
            env={"TELEGRAM_BOT_TOKEN": "secret-token"},
            opener=opener,
        )

        self.assertEqual(output["status"], "NO_DATA")
        self.assertEqual(output["reason"], "FINAM_ARENA_CALLBACK_POLLING_ENABLED_not_enabled")
        self.assertEqual(opener.call_count, 0)
        self.assertFalse(output["safety"]["trading_mutations"])


if __name__ == "__main__":
    unittest.main()
