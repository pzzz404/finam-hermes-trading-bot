import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location("runtime_doctor_script", ROOT / "scripts" / "runtime_doctor.py")
runtime_doctor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runtime_doctor)


class RuntimeDoctorTests(unittest.TestCase):
    def complete_env(self):
        return {
            "FINAM_TOKEN": "finam-secret-value",
            "FINAM_ARENA_API": "arena-secret-value",
            "FINAM_ACCOUNT_ID": "DEMO-ACCOUNT",
            "TELEGRAM_BOT_TOKEN": "telegram-secret-value",
            "FINAM_H4_TELEGRAM_CHAT_ID": "-100123",
            "NO_PROXY": "localhost,127.0.0.1,finam.ru,.finam.ru,api.finam.ru,www.finam.ru",
        }

    def test_skip_network_reports_safe_env_lengths_without_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_doctor.run_diagnostics(
                env=self.complete_env(),
                skip_network=True,
                outbox_root=Path(tmp) / "outbox",
                safety_state_path=Path(tmp) / "safety.json",
            )

        payload = json.dumps(result, ensure_ascii=False)
        self.assertEqual(result["status"], "OK")
        self.assertIn('"length"', payload)
        self.assertNotIn("finam-secret-value", payload)
        self.assertNotIn("arena-secret-value", payload)
        self.assertNotIn("telegram-secret-value", payload)
        self.assertNotIn("user:pass", payload)

    def test_missing_required_env_blocks_trading_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_doctor.run_diagnostics(
                env={},
                skip_network=True,
                outbox_root=Path(tmp) / "outbox",
                safety_state_path=Path(tmp) / "safety.json",
            )

        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIn("FINAM_TOKEN is not set", result["errors"])
        self.assertIn("FINAM_ARENA_API is not set", result["errors"])
        self.assertIn("TELEGRAM_BOT_TOKEN is not set", result["errors"])

    def test_halt_safety_state_blocks_new_buys(self):
        with tempfile.TemporaryDirectory() as tmp:
            safety = Path(tmp) / "safety.json"
            safety.write_text(json.dumps({"halt_new_buys": True, "status": "STOP_NOT_VERIFIED"}), encoding="utf-8")

            result = runtime_doctor.run_diagnostics(
                env=self.complete_env(),
                skip_network=True,
                outbox_root=Path(tmp) / "outbox",
                safety_state_path=safety,
            )

        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIn("trade safety state blocks new buys", result["errors"])

    def test_arena_preflight_checks_three_accounts(self):
        class FakeArenaClient:
            def __init__(self, *, base_url="https://api.finam.ru"):
                self.base_url = base_url

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"account_id": account_id, "status": "ACCOUNT_ACTIVE"}

        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        with mock.patch.object(runtime_doctor, "FinamClient", FakeArenaClient):
            runtime_doctor._check_arena(result, self.complete_env())

        self.assertTrue(result["checks"]["arena"]["ok"])
        self.assertEqual(result["checks"]["arena"]["base_url"], "https://arena.finam.ru")
        self.assertEqual(result["checks"]["arena"]["account_ids"], ["DEMO-RU", "DEMO-US", "DEMO-AI"])
        self.assertEqual(result["errors"], [])

    def test_arena_preflight_blocks_inactive_account(self):
        class FakeArenaClient:
            def __init__(self, *, base_url="https://api.finam.ru"):
                self.base_url = base_url

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                status = "ACCOUNT_BLOCKED" if account_id == "DEMO-US" else "ACCOUNT_ACTIVE"
                return {"account_id": account_id, "status": status}

        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        with mock.patch.object(runtime_doctor, "FinamClient", FakeArenaClient):
            runtime_doctor._check_arena(result, self.complete_env())

        self.assertFalse(result["checks"]["arena"]["ok"])
        self.assertEqual(result["checks"]["arena"]["account_statuses"]["DEMO-US"], "ACCOUNT_BLOCKED")
        self.assertTrue(any("Arena account is not active" in item for item in result["errors"]))

    def test_arena_preflight_treats_readable_account_without_status_as_active(self):
        class FakeArenaClient:
            def __init__(self, *, base_url="https://api.finam.ru"):
                self.base_url = base_url

            def create_session(self, secret):
                return "arena-jwt"

            def get_account(self, jwt, account_id):
                return {"account_id": account_id, "equity": {"value": "1000000"}, "positions": []}

        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        with mock.patch.object(runtime_doctor, "FinamClient", FakeArenaClient):
            runtime_doctor._check_arena(result, self.complete_env())

        self.assertTrue(result["checks"]["arena"]["ok"])
        self.assertEqual(result["checks"]["arena"]["account_statuses"]["DEMO-RU"], "ACCOUNT_ACTIVE")
        self.assertEqual(result["errors"], [])

    def test_regular_finam_failure_is_warning_in_arena_first_runtime(self):
        class FakeFinamClient:
            def create_session(self, secret):
                return "jwt"

            def session_details(self, jwt):
                return {"readonly": False}

            def get_account(self, jwt, account_id):
                raise RuntimeError("Account with id DEMO-ACCOUNT is not found")

        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        with mock.patch.object(runtime_doctor, "FinamClient", FakeFinamClient):
            runtime_doctor._check_finam(result, self.complete_env())

        self.assertEqual(result["errors"], [])
        self.assertFalse(result["checks"]["finam"]["fatal"])
        self.assertTrue(result["checks"]["finam"]["arena_mode"])
        self.assertTrue(any("ignored for Arena-first runtime" in item for item in result["warnings"]))

    def test_regular_finam_failure_is_error_without_arena_runtime(self):
        class FakeFinamClient:
            def create_session(self, secret):
                return "jwt"

            def session_details(self, jwt):
                return {"readonly": False}

            def get_account(self, jwt, account_id):
                raise RuntimeError("Account with id DEMO-ACCOUNT is not found")

        env = self.complete_env()
        env["FINAM_ARENA_API"] = ""
        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        with mock.patch.object(runtime_doctor, "FinamClient", FakeFinamClient):
            runtime_doctor._check_finam(result, env)

        self.assertTrue(result["checks"]["finam"]["fatal"])
        self.assertTrue(any("Finam API:" in item for item in result["errors"]))
        self.assertEqual(result["warnings"], [])

    def test_missing_finam_account_id_is_skipped_in_arena_first_runtime(self):
        env = self.complete_env()
        env["FINAM_ACCOUNT_ID"] = ""
        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        runtime_doctor._check_finam(result, env)

        self.assertEqual(result["errors"], [])
        self.assertTrue(result["checks"]["finam"]["skipped"])
        self.assertFalse(result["checks"]["finam"]["fatal"])

    def test_missing_finam_account_id_is_error_without_arena_runtime(self):
        env = self.complete_env()
        env["FINAM_ARENA_API"] = ""
        env["FINAM_ACCOUNT_ID"] = ""
        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        runtime_doctor._check_finam(result, env)

        self.assertIn("FINAM_ACCOUNT_ID is not set", result["errors"])

    def test_finam_usage_is_best_effort(self):
        class FakeFinamClient:
            def create_session(self, secret):
                return "jwt"

            def usage(self, jwt):
                return {"quotas": [{"name": "requests_per_minute", "limit": 200, "used": 12}]}

        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        with mock.patch.object(runtime_doctor, "FinamClient", FakeFinamClient):
            runtime_doctor._check_finam_usage(result, self.complete_env())

        self.assertTrue(result["checks"]["finam_usage"]["ok"])
        self.assertEqual(result["checks"]["finam_usage"]["quotas"][0]["limit"], 200)
        self.assertEqual(result["errors"], [])

    def test_finam_usage_failure_does_not_add_runtime_error(self):
        class FakeFinamClient:
            def create_session(self, secret):
                return "jwt"

            def usage(self, jwt):
                raise RuntimeError("usage endpoint unavailable")

        result = {"status": "OK", "checks": {}, "errors": [], "warnings": []}

        with mock.patch.object(runtime_doctor, "FinamClient", FakeFinamClient):
            runtime_doctor._check_finam_usage(result, self.complete_env())

        self.assertFalse(result["checks"]["finam_usage"]["ok"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["warnings"], [])

    def test_proxy_config_requires_direct_finam_bypass(self):
        env = self.complete_env()
        env["HTTPS_PROXY"] = "http://proxy.example:8080"
        env["NO_PROXY"] = "localhost,127.0.0.1"
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_doctor.run_diagnostics(
                env=env,
                skip_network=True,
                outbox_root=Path(tmp) / "outbox",
                safety_state_path=Path(tmp) / "safety.json",
            )

        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIn("Finam hosts must bypass proxy/VPN via NO_PROXY", result["errors"])
        self.assertFalse(result["checks"]["proxy"]["finam_direct"]["api.finam.ru"])

    def test_expected_proxy_is_optional_and_checked_when_configured(self):
        env = self.complete_env()
        env["HTTPS_PROXY"] = "http://proxy.example:8080"
        env["FINAM_EXPECTED_PROXY"] = "http://proxy.example:9090"
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_doctor.run_diagnostics(
                env=env,
                skip_network=True,
                outbox_root=Path(tmp) / "outbox",
                safety_state_path=Path(tmp) / "safety.json",
            )

        self.assertEqual(result["status"], "NO_TRADE")
        self.assertIn("HTTPS_PROXY must match FINAM_EXPECTED_PROXY", result["errors"])
        self.assertTrue(result["checks"]["proxy"]["route_matrix"]["telegram"]["ok"])


if __name__ == "__main__":
    unittest.main()
