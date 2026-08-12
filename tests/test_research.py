import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finam_trading_bot import research  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self):
        return self.payload


class ResearchTests(unittest.TestCase):
    def test_verdict_from_summary_detects_avoid_and_risk(self):
        self.assertEqual(research.verdict_from_summary("PLZL@MISX: AVOID дивидендный риск", ["PLZL@MISX"]), "AVOID")
        self.assertEqual(research.verdict_from_summary("TATN: RISK новости слабые", ["TATN@MISX"]), "RISK")
        self.assertEqual(research.verdict_from_summary("MOEX: OK", ["MOEX@MISX"]), "OK")

    def test_openrouter_key_does_not_trigger_provider_call(self):
        calls = []

        def opener(url, timeout):
            calls.append(str(getattr(url, "full_url", url)))
            return FakeResponse(b"<rss><channel></channel></rss>")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            result = research.research_candidates(
                [{"symbol": "SBER@MISX"}, {"symbol": "GAZP@MISX"}],
                {"research": {"enabled": True, "finam_rss_url": "https://www.finam.ru/rss.xml"}},
                urlopen=opener,
                cache_root=Path(tmp),
            )

        self.assertEqual(result["provider"], "finam_rss")
        self.assertEqual(result["verdict"], "UNAVAILABLE")
        self.assertFalse(result.get("provider_call"))
        self.assertFalse(any("openrouter.ai" in call for call in calls))
        self.assertNotIn("real-key", json.dumps(result, ensure_ascii=False))

    def test_openrouter_provider_is_retired_for_h4_news_gate(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            result = research.research_candidates(
                [{"symbol": "SBER@MISX"}, {"symbol": "GAZP@MISX"}, {"symbol": "LKOH@MISX"}],
                {"research": {"enabled": True, "provider": "openrouter", "finam_rss_enabled": False, "h4_max_candidates": 2, "h4_max_tokens": 350}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                cache_root=Path(tmp),
            )

        self.assertEqual(result["status"], "codex_review_required")
        self.assertEqual(result["provider"], "codex_review")
        self.assertEqual(result["mode"], "h4_news_gate")
        self.assertEqual(result["symbols"], ["SBER@MISX", "GAZP@MISX"])
        self.assertEqual(result["reason"], "expert_verdict_requires_recorded_codex_review")
        self.assertEqual(result["requested_max_tokens"], 350)
        self.assertFalse(result["provider_call"])

    def test_h4_news_gate_can_reuse_cache_without_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            first = research.research_candidates(
                [{"symbol": "SBER@MISX"}],
                {"research": {"enabled": True, "provider": "openrouter", "finam_rss_enabled": False, "h4_model": "perplexity/sonar"}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                cache_root=Path(tmp),
            )
            second = research.research_candidates(
                [{"symbol": "SBER@MISX"}],
                {"research": {"enabled": True, "provider": "openrouter", "finam_rss_enabled": False, "h4_model": "perplexity/sonar"}},
                urlopen=mock.Mock(side_effect=AssertionError("provider should not be called")),
                cache_root=Path(tmp),
                cache_only=True,
            )

        self.assertEqual(first["status"], "codex_review_required")
        self.assertEqual(first["reason"], "expert_verdict_requires_recorded_codex_review")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["provider"], "openrouter")
        self.assertFalse(second["provider_call"])

    def test_h4_news_gate_defaults_to_codex_review_even_with_legacy_model_keys(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            result = research.research_candidates(
                [{"symbol": "SBER@MISX"}],
                {"research": {"enabled": True, "model": "perplexity/sonar-pro", "finam_rss_enabled": False}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                cache_root=Path(tmp),
            )

        self.assertEqual(result["status"], "codex_review_required")
        self.assertEqual(result["provider"], "codex_review")
        self.assertFalse(result["provider_call"])

    def test_mode_specific_models_keep_pretrade_on_pro_model(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            result = research.pretrade_check(
                "SBER@MISX",
                {"research": {"enabled": True, "h4_model": "perplexity/sonar", "pretrade_model": "perplexity/sonar-pro"}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                budget_root=Path(tmp) / "budget",
            )

        self.assertEqual(result["status"], "codex_review_required")
        self.assertEqual(result["provider"], "codex_review")
        self.assertEqual(result["retired_provider_model"], "perplexity/sonar-pro")
        self.assertFalse(result["provider_call"])

    def test_pretrade_check_writes_artifact_and_reuses_it_without_budget_spend(self):
        now = datetime(2026, 6, 16, 13, 20, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            root = Path(tmp)
            budget_root = root / "openrouter_budget"
            first = research.pretrade_check(
                "NVDA@XNGS",
                {"research": {"enabled": True, "pretrade_daily_request_budget": 4}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                budget_root=budget_root,
                now=now,
            )
            second = research.pretrade_check(
                "NVDA@XNGS",
                {"research": {"enabled": True, "pretrade_daily_request_budget": 4}},
                urlopen=mock.Mock(side_effect=AssertionError("pretrade cache hit must not call provider")),
                budget_root=budget_root,
                now=now + timedelta(minutes=10),
            )

        self.assertEqual(first["status"], "codex_review_required")
        self.assertEqual(first["reason"], "expert_verdict_requires_recorded_codex_review")
        self.assertEqual(second["status"], "codex_review_required")
        self.assertFalse(second["provider_call"])

    def test_pretrade_check_ignores_expired_artifact_and_requires_codex_review(self):
        now = datetime(2026, 6, 16, 13, 20, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            root = Path(tmp)
            pretrade_dir = root / "pretrade"
            pretrade_dir.mkdir()
            (pretrade_dir / "NVDA@XNGS_20260616T080000Z.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": "openrouter",
                        "mode": "pretrade_check",
                        "symbol": "NVDA@XNGS",
                        "symbols": ["NVDA@XNGS"],
                        "verdict": "RISK",
                        "expires_at": "2026-06-16T09:00:00+00:00",
                        "provider_call": True,
                        "cache_hit": False,
                    }
                ),
                encoding="utf-8",
            )
            result = research.pretrade_check(
                "NVDA@XNGS",
                {"research": {"enabled": True, "pretrade_daily_request_budget": 4}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                budget_root=root / "openrouter_budget",
                now=now,
            )

        self.assertEqual(result["status"], "codex_review_required")
        self.assertEqual(result["reason"], "expert_verdict_requires_recorded_codex_review")
        self.assertFalse(result["cache_hit"])
        self.assertFalse(result["provider_call"])

    def test_pretrade_check_reuses_risk_artifact_without_budget_spend(self):
        now = datetime(2026, 6, 16, 13, 20, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            root = Path(tmp)
            pretrade_dir = root / "pretrade"
            pretrade_dir.mkdir()
            (pretrade_dir / "NVDA@XNGS_20260616T120000Z.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "provider": "openrouter",
                        "mode": "pretrade_check",
                        "symbol": "NVDA@XNGS",
                        "symbols": ["NVDA@XNGS"],
                        "verdict": "RISK",
                        "expires_at": "2026-06-16T16:00:00+00:00",
                        "provider_call": True,
                        "cache_hit": False,
                    }
                ),
                encoding="utf-8",
            )
            result = research.pretrade_check(
                "NVDA@XNGS",
                {"research": {"enabled": True, "pretrade_daily_request_budget": 4}},
                urlopen=mock.Mock(side_effect=AssertionError("risk artifact must be reused")),
                budget_root=root / "openrouter_budget",
                now=now,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["verdict"], "RISK")
        self.assertTrue(result["cache_hit"])
        self.assertFalse(result["provider_call"])

    def test_daily_digest_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            result = research.daily_digest(
                {"research": {"enabled": True, "daily_max_symbols": 12}},
                ["SBER@MISX", "MOEX@MISX"],
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                budget_root=Path(tmp) / "budget",
            )

        with tempfile.TemporaryDirectory() as tmp:
            artifact = research.write_research_artifact("daily", result, root=Path(tmp))
            self.assertTrue(Path(artifact["json_path"]).exists())
            self.assertTrue(Path(artifact["md_path"]).exists())
            latest = research.latest_research_artifact("daily", root=Path(tmp))

        self.assertEqual(latest["status"], "codex_review_required")
        self.assertEqual(latest["reason"], "expert_verdict_requires_recorded_codex_review")

    def test_research_candidates_deduplicates_symbols_before_prompt_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            result = research.research_candidates(
                [{"symbol": "AAPL@XNGS"}, {"symbol": "AAPL@XNGS"}],
                {"research": {"enabled": True, "provider": "openrouter", "finam_rss_enabled": False, "h4_max_candidates": 2}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                cache_root=Path(tmp),
            )

        self.assertEqual(result["symbols"], ["AAPL@XNGS"])
        self.assertEqual(result["status"], "codex_review_required")
        self.assertFalse(result["provider_call"])

    def test_h4_news_gate_daily_budget_exhaustion_prevents_provider_call(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            root = Path(tmp)
            budget_dir = root / "openrouter_budget"
            budget_dir.mkdir()
            today = datetime.now(timezone.utc).date().isoformat()
            (budget_dir / f"{today}.json").write_text(
                json.dumps(
                    {
                        "period": today,
                        "calls": [
                            {"timestamp": datetime.now(timezone.utc).isoformat(), "mode": "h4_news_gate", "model": "perplexity/sonar", "symbols": ["SBER@MISX"]}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = research.research_candidates(
                [{"symbol": "SBER@MISX"}],
                {"research": {"enabled": True, "h4_daily_request_budget": 1}},
                urlopen=mock.Mock(side_effect=AssertionError("provider should not be called")),
                cache_root=root,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["provider"], "finam_rss")
        self.assertFalse(result.get("provider_call", False))

    def test_dedicated_research_commands_respect_disabled_policy(self):
        opener = mock.Mock()

        daily = research.daily_digest({"research": {"enabled": False}}, ["SBER@MISX"], urlopen=opener)
        weekly = research.weekly_review({"research": {"enabled": False}}, ["SBER@MISX"], urlopen=opener)
        pretrade = research.pretrade_check("SBER@MISX", {"research": {"enabled": False}}, urlopen=opener)

        self.assertEqual(daily["status"], "disabled")
        self.assertEqual(weekly["status"], "disabled")
        self.assertEqual(pretrade["status"], "disabled")
        opener.assert_not_called()

    def test_finam_rss_fallback_when_openrouter_key_missing(self):
        rss = """<?xml version="1.0" encoding="utf-8"?>
        <rss><channel>
          <item><title>MOEX: операционные результаты без негативных сюрпризов</title></item>
          <item><title>PLZL: дивидендный гэп риск сохраняется</title></item>
        </channel></rss>""".encode("utf-8")

        def opener(url, timeout):
            self.assertIn("finam.ru", url)
            return FakeResponse(rss)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            result = research.research_candidates(
                [{"symbol": "PLZL@MISX"}],
                {"research": {"enabled": True, "finam_rss_url": "https://www.finam.ru/rss.xml"}},
                urlopen=opener,
                cache_root=Path(tmp),
            )

        self.assertEqual(result["status"], "facts")
        self.assertEqual(result["provider"], "finam_rss")
        self.assertEqual(result["verdict"], "UNAVAILABLE")
        self.assertEqual(result["rss_flags"], ["PLZL: дивидендный гэп риск сохраняется"])

    def test_research_disabled_returns_unavailable_without_provider_call(self):
        calls = []

        def opener(url, timeout):
            calls.append(url)
            return FakeResponse(b"")

        result = research.research_candidates([{"symbol": "SBER@MISX"}], {"research": {"enabled": False}}, urlopen=opener)

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["verdict"], "UNAVAILABLE")
        self.assertEqual(calls, [])

    def test_rss_disabled_returns_codex_review_required(self):
        calls = []

        def opener(url, timeout):
            calls.append(url)
            return FakeResponse(b"")

        result = research.research_candidates(
            [{"symbol": "SBER@MISX"}],
            {"research": {"enabled": True, "finam_rss_enabled": False}},
            urlopen=opener,
        )

        self.assertEqual(result["status"], "codex_review_required")
        self.assertEqual(result["provider"], "codex_review")
        self.assertFalse(result["provider_call"])
        self.assertEqual(calls, [])

    def test_openrouter_provider_retired_before_provider_client_call(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}, clear=True):
            result = research.research_candidates(
                [{"symbol": "SBER@MISX"}],
                {"research": {"enabled": True, "provider": "openrouter", "finam_rss_enabled": False}},
                urlopen=mock.Mock(side_effect=AssertionError("OpenRouter must not be called")),
                cache_root=Path(tmp),
            )

        self.assertEqual(result["status"], "codex_review_required")
        self.assertEqual(result["provider"], "codex_review")
        self.assertEqual(result["reason"], "expert_verdict_requires_recorded_codex_review")
        self.assertFalse(result["provider_call"])

    def test_rss_parser_strips_html(self):
        raw = """<rss><channel><item><title>SBER</title><description>AAA <a href="x">link</a></description></item></channel></rss>"""

        self.assertEqual(research._parse_rss_headlines(raw), ["SBER AAA"])


if __name__ == "__main__":
    unittest.main()
