"""Local facts layer for H4 trade candidates.

Trading expertise now comes from Hermes's built-in Codex review protocol. This
module deliberately does not call OpenRouter, Perplexity, or Sonar; it only
collects compact factual RSS flags that may be referenced by a Codex prompt.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
import fcntl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import unescape
from pathlib import Path
from typing import Any, Callable


UrlOpen = Callable[..., Any]

DEFAULT_FINAM_RSS_URL = "https://www.finam.ru/analysis/conews/rsspoint/"
DEFAULT_RESEARCH_ROOT = Path(__file__).resolve().parents[2] / "data" / "runtime" / "research"
DEFAULT_OPENROUTER_BUDGET_ROOT = DEFAULT_RESEARCH_ROOT / "openrouter_budget"
VERDICTS = {"OK", "RISK", "AVOID", "UNAVAILABLE"}
RESEARCH_MODES = {"h4_news_gate", "daily_digest", "weekly_review", "pretrade_check"}


@dataclass(frozen=True)
class ResearchConfig:
    enabled: bool = True
    provider: str = "codex_review"
    model: str = "openai-codex"
    h4_model: str = "perplexity/sonar"
    daily_model: str = "perplexity/sonar-pro"
    weekly_model: str = "perplexity/sonar-pro"
    pretrade_model: str = "perplexity/sonar-pro"
    timeout_seconds: int = 20
    max_candidates: int = 1
    h4_max_candidates: int = 2
    h4_max_tokens: int = 350
    daily_max_tokens: int = 1000
    weekly_max_tokens: int = 1600
    pretrade_max_tokens: int = 800
    daily_max_symbols: int = 12
    weekly_max_symbols: int = 24
    h4_cache_ttl_seconds: int = 1800
    finam_rss_enabled: bool = True
    finam_rss_url: str = DEFAULT_FINAM_RSS_URL
    h4_daily_request_budget: int = 6
    pretrade_daily_request_budget: int = 4
    daily_digest_request_budget: int = 1
    weekly_review_request_budget: int = 1


def research_candidates(
    candidates: list[dict[str, Any]],
    policy: dict[str, Any],
    *,
    mode: str = "h4_news_gate",
    urlopen: UrlOpen | None = None,
    cache_only: bool = False,
    cache_root: Path = DEFAULT_RESEARCH_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    config = _research_config(research)
    if not config.enabled:
        return {"status": "disabled", "verdict": "UNAVAILABLE"}
    if not candidates:
        return {"status": "skipped", "reason": "no_trade_candidates", "verdict": "UNAVAILABLE"}

    opener = urlopen or urllib.request.urlopen
    if mode not in RESEARCH_MODES:
        return {"status": "failed", "reason": f"unknown_research_mode:{mode}", "verdict": "UNAVAILABLE"}

    max_symbols = config.h4_max_candidates if mode == "h4_news_gate" else config.max_candidates
    symbols = _dedupe_symbols([str(item.get("symbol")) for item in candidates], max_symbols=max_symbols)
    cached = _read_h4_cache(symbols, config, cache_root=cache_root, mode=mode, now=now)
    if cached is not None:
        return cached
    if cache_only and mode == "h4_news_gate":
        return {
            "status": "skipped",
            "provider": config.provider,
            "mode": mode,
            "reason": "h4_research_cache_miss",
            "symbols": symbols,
            "verdict": "UNAVAILABLE",
            "cache": {"status": "miss"},
            "cache_hit": False,
            "provider_call": False,
            "model": _model_for_mode(config, mode),
            "requested_max_tokens": _max_tokens_for_mode(config, mode),
        }

    if config.provider == "codex_review":
        if config.finam_rss_enabled:
            rss_result = _finam_rss_research(symbols, config, opener=opener)
            if rss_result.get("status") in {"facts", "failed"}:
                return rss_result
        return {
            "status": "codex_review_required",
            "provider": "codex_review",
            "reason": "expert_verdict_requires_recorded_codex_review",
            "symbols": symbols,
            "provider_call": False,
            "verdict": "UNAVAILABLE",
            "rss_flags": [],
        }

    openrouter_result = _openrouter_research(
        symbols,
        config,
        opener=opener,
        mode=mode,
        budget_root=cache_root / "openrouter_budget",
        now=now,
    )
    if openrouter_result.get("status") == "ok":
        _write_h4_cache(openrouter_result, symbols, config, cache_root=cache_root, mode=mode, now=now)
        return openrouter_result
    if openrouter_result.get("reason") == "openrouter_daily_budget_exhausted":
        return openrouter_result

    if mode == "h4_news_gate" and config.finam_rss_enabled:
        rss_result = _finam_rss_research(symbols, config, opener=opener)
        if rss_result.get("status") in {"facts", "failed"}:
            rss_result["fallback_from"] = openrouter_result
            _write_h4_cache(rss_result, symbols, config, cache_root=cache_root, mode=mode, now=now)
            return rss_result

    return openrouter_result


def daily_digest(
    policy: dict[str, Any],
    symbols: list[str],
    *,
    urlopen: UrlOpen | None = None,
    now: datetime | None = None,
    budget_root: Path = DEFAULT_OPENROUTER_BUDGET_ROOT,
) -> dict[str, Any]:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    config = _research_config(research)
    if not config.enabled:
        return {"status": "disabled", "mode": "daily_digest", "verdict": "UNAVAILABLE"}
    symbols = _dedupe_symbols([str(item) for item in symbols], max_symbols=config.daily_max_symbols)
    result = _openrouter_research(symbols, config, opener=urlopen or urllib.request.urlopen, mode="daily_digest", budget_root=budget_root, now=now)
    if result.get("status") == "ok":
        today = (now or datetime.now(timezone.utc)).date().isoformat()
        result.setdefault("date", today)
        result.setdefault("expires_at", _iso_utc((now or datetime.now(timezone.utc)) + timedelta(hours=18)))
    return result


def weekly_review(
    policy: dict[str, Any],
    symbols: list[str],
    *,
    urlopen: UrlOpen | None = None,
    now: datetime | None = None,
    budget_root: Path = DEFAULT_OPENROUTER_BUDGET_ROOT,
) -> dict[str, Any]:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    config = _research_config(research)
    if not config.enabled:
        return {"status": "disabled", "mode": "weekly_review", "verdict": "UNAVAILABLE"}
    symbols = _dedupe_symbols([str(item) for item in symbols], max_symbols=config.weekly_max_symbols)
    now_utc = now or datetime.now(timezone.utc)
    result = _openrouter_research(symbols, config, opener=urlopen or urllib.request.urlopen, mode="weekly_review", budget_root=budget_root, now=now_utc)
    if result.get("status") == "ok":
        result.setdefault("week", _iso_week(now_utc))
        result.setdefault("expires_at", _iso_utc(now_utc + timedelta(days=7)))
    return result


def pretrade_check(
    symbol: str,
    policy: dict[str, Any],
    *,
    urlopen: UrlOpen | None = None,
    now: datetime | None = None,
    budget_root: Path = DEFAULT_OPENROUTER_BUDGET_ROOT,
) -> dict[str, Any]:
    research = policy.get("research") if isinstance(policy.get("research"), dict) else {}
    config = _research_config(research)
    if not config.enabled:
        return {"status": "disabled", "mode": "pretrade_check", "symbol": symbol, "verdict": "UNAVAILABLE"}
    symbols = _dedupe_symbols([symbol], max_symbols=1)
    normalized_symbol = symbols[0] if symbols else str(symbol)
    artifact_root = budget_root.parent
    cached = _latest_pretrade_artifact(normalized_symbol, root=artifact_root, now=now)
    if cached is not None:
        return cached
    result = _openrouter_research(symbols, config, opener=urlopen or urllib.request.urlopen, mode="pretrade_check", budget_root=budget_root, now=now)
    if result.get("status") == "ok":
        result.setdefault("symbol", normalized_symbol)
        result.setdefault("expires_at", _iso_utc((now or datetime.now(timezone.utc)) + timedelta(hours=4)))
        try:
            artifact = write_research_artifact("pretrade", result, root=artifact_root)
            result["artifact"] = {"json_path": artifact["json_path"], "md_path": artifact["md_path"]}
        except OSError as exc:
            result["artifact_error"] = str(exc)[:200]
    return result


def write_research_artifact(kind: str, payload: dict[str, Any], *, root: Path = DEFAULT_RESEARCH_ROOT) -> dict[str, Any]:
    if kind not in {"daily", "weekly", "pretrade"}:
        raise ValueError("kind must be daily, weekly, or pretrade")
    if kind == "daily":
        name = str(payload.get("date") or datetime.now(timezone.utc).date().isoformat())
    elif kind == "weekly":
        name = str(payload.get("week") or _iso_week(datetime.now(timezone.utc)))
    else:
        symbol = str(payload.get("symbol") or "UNKNOWN").replace("/", "_")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{symbol}_{stamp}"
    target_dir = root / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / f"{name}.json"
    md_path = target_dir / f"{name}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_artifact_markdown(kind, payload), encoding="utf-8")
    return {"kind": kind, "json_path": str(json_path), "md_path": str(md_path), "payload": payload}


def latest_research_artifact(kind: str, *, root: Path = DEFAULT_RESEARCH_ROOT, now: datetime | None = None) -> dict[str, Any] | None:
    target_dir = root / kind
    if not target_dir.exists():
        return None
    files = sorted(target_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    now_utc = now or datetime.now(timezone.utc)
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        expires_at = _parse_iso_datetime(payload.get("expires_at"))
        if expires_at is not None and expires_at < now_utc:
            continue
        payload["_artifact_path"] = str(path)
        return payload
    return None


def _latest_pretrade_artifact(symbol: str, *, root: Path, now: datetime | None = None) -> dict[str, Any] | None:
    target_dir = root / "pretrade"
    if not target_dir.exists():
        return None
    safe_symbol = str(symbol or "UNKNOWN").replace("/", "_")
    files = sorted(target_dir.glob(f"{safe_symbol}_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    now_utc = now or datetime.now(timezone.utc)
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("mode") != "pretrade_check":
            continue
        payload_symbol = str(payload.get("symbol") or (payload.get("symbols") or [""])[0]).upper()
        if payload_symbol != str(symbol or "").upper():
            continue
        expires_at = _parse_iso_datetime(payload.get("expires_at"))
        if expires_at is None or expires_at < now_utc:
            continue
        cached = dict(payload)
        cached["_artifact_path"] = str(path)
        cached["cache_hit"] = True
        cached["provider_call"] = False
        return cached
    return None


def verdict_from_summary(summary: str, symbols: list[str]) -> str:
    upper = summary.upper()
    found = "OK"
    for symbol in symbols:
        ticker = symbol.split("@", 1)[0].upper()
        positions = [pos for pos in (upper.find(symbol.upper()), upper.find(ticker)) if pos >= 0]
        if not positions:
            continue
        window = upper[min(positions) : min(positions) + 240]
        if "AVOID" in window:
            return "AVOID"
        if "RISK" in window:
            found = "RISK"
    return found


def _dedupe_symbols(symbols: list[str], *, max_symbols: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip()
        if not symbol:
            continue
        key = symbol.upper()
        if key in seen:
            continue
        seen.add(key)
        result.append(symbol)
        if len(result) >= max_symbols:
            break
    return result


def _research_config(research: dict[str, Any]) -> ResearchConfig:
    provider = str(research.get("provider") or "codex_review")
    return ResearchConfig(
        enabled=bool(research.get("enabled", True)),
        provider=provider,
        model=str(research.get("model") or ("perplexity/sonar-pro" if provider == "openrouter" else "openai-codex")),
        h4_model=str(research.get("h4_model") or "perplexity/sonar"),
        daily_model=str(research.get("daily_model") or research.get("model") or "perplexity/sonar-pro"),
        weekly_model=str(research.get("weekly_model") or research.get("model") or "perplexity/sonar-pro"),
        pretrade_model=str(research.get("pretrade_model") or research.get("model") or "perplexity/sonar-pro"),
        timeout_seconds=_positive_int(research.get("timeout_seconds"), default=20),
        max_candidates=_positive_int(research.get("max_candidates"), default=1),
        h4_max_candidates=_positive_int(research.get("h4_max_candidates"), default=2),
        h4_max_tokens=_positive_int(research.get("h4_max_tokens"), default=350),
        daily_max_tokens=_positive_int(research.get("daily_max_tokens"), default=1000),
        weekly_max_tokens=_positive_int(research.get("weekly_max_tokens"), default=1600),
        pretrade_max_tokens=_positive_int(research.get("pretrade_max_tokens"), default=800),
        daily_max_symbols=_positive_int(research.get("daily_max_symbols"), default=12),
        weekly_max_symbols=_positive_int(research.get("weekly_max_symbols"), default=24),
        h4_cache_ttl_seconds=_positive_int(research.get("h4_cache_ttl_seconds"), default=1800),
        finam_rss_enabled=bool(research.get("finam_rss_enabled", True)),
        finam_rss_url=str(research.get("finam_rss_url") or DEFAULT_FINAM_RSS_URL),
        h4_daily_request_budget=_positive_int(research.get("h4_daily_request_budget"), default=6),
        pretrade_daily_request_budget=_positive_int(research.get("pretrade_daily_request_budget"), default=4),
        daily_digest_request_budget=_positive_int(research.get("daily_digest_request_budget"), default=1),
        weekly_review_request_budget=_positive_int(research.get("weekly_review_request_budget"), default=1),
    )


def _reserve_openrouter_budget(
    symbols: list[str],
    config: ResearchConfig,
    *,
    mode: str,
    model: str,
    budget_root: Path,
    now: datetime | None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    limit = _budget_limit_for_mode(config, mode)
    period = _budget_period_for_mode(mode, current)
    path = budget_root / f"{period}.json"
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            state = _read_budget_state(path, period=period)
            calls = [item for item in state.get("calls") or [] if isinstance(item, dict)]
            used = len([item for item in calls if str(item.get("mode") or "") == mode])
            spent_symbols = _budget_spent_symbols(calls, mode=mode)
            remaining_before = max(0, limit - used)
            if remaining_before <= 0:
                return {
                    "status": "exhausted",
                    "reason": "openrouter_daily_budget_exhausted",
                    "period": period,
                    "limit": limit,
                    "used": used,
                    "remaining": 0,
                    "spent_symbols": spent_symbols,
                }
            record = {
                "timestamp": current.isoformat(timespec="seconds"),
                "mode": mode,
                "model": model,
                "symbols": symbols,
            }
            calls.append(record)
            state["calls"] = calls
            state["limits"] = dict(state.get("limits") or {}) | {mode: limit}
            _write_budget_state(path, state)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        return {
            "status": "failed",
            "reason": "openrouter_budget_write_failed",
            "error": str(exc)[:200],
            "period": period,
            "limit": limit,
            "used": None,
            "remaining": 0,
            "spent_symbols": [],
        }
    return {
        "status": "ok",
        "period": period,
        "limit": limit,
        "used": used + 1,
        "remaining": max(0, limit - used - 1),
        "spent_symbols": _budget_spent_symbols(calls, mode=mode),
    }


def _read_budget_state(path: Path, *, period: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if not isinstance(payload.get("calls"), list):
        payload["calls"] = []
    payload["period"] = str(payload.get("period") or period)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


def _write_budget_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _budget_limit_for_mode(config: ResearchConfig, mode: str) -> int:
    if mode == "h4_news_gate":
        return config.h4_daily_request_budget
    if mode == "pretrade_check":
        return config.pretrade_daily_request_budget
    if mode == "daily_digest":
        return config.daily_digest_request_budget
    if mode == "weekly_review":
        return config.weekly_review_request_budget
    return 1


def _budget_period_for_mode(mode: str, now: datetime) -> str:
    if mode == "weekly_review":
        return _iso_week(now)
    return now.date().isoformat()


def _budget_payload_fields(budget: dict[str, Any]) -> dict[str, Any]:
    return {
        "budget_period": budget.get("period"),
        "budget_limit": budget.get("limit"),
        "budget_used": budget.get("used"),
        "budget_remaining": budget.get("remaining"),
        "budget_spent_symbols": budget.get("spent_symbols"),
    }


def _budget_spent_symbols(calls: list[dict[str, Any]], *, mode: str) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for item in calls:
        if str(item.get("mode") or "") != mode:
            continue
        for symbol in item.get("symbols") or []:
            normalized = str(symbol or "").upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                symbols.append(normalized)
    return symbols


def _openrouter_research(
    symbols: list[str],
    config: ResearchConfig,
    *,
    opener: UrlOpen,
    mode: str,
    budget_root: Path = DEFAULT_OPENROUTER_BUDGET_ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    config_model = _model_for_mode(config, mode)
    return {
        "status": "codex_review_required",
        "provider": "codex_review",
        "mode": mode,
        "reason": "expert_verdict_requires_recorded_codex_review",
        "symbols": symbols,
        "verdict": "UNAVAILABLE",
        "model": "openai-codex",
        "retired_provider_model": config_model,
        "requested_max_tokens": _max_tokens_for_mode(config, mode),
        "provider_call": False,
        "cache_hit": False,
    }
def _finam_rss_research(symbols: list[str], config: ResearchConfig, *, opener: UrlOpen) -> dict[str, Any]:
    try:
        with opener(config.finam_rss_url, timeout=min(config.timeout_seconds, 20)) as response:
            raw = response.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - fallback can degrade.
        return {
            "status": "failed",
            "provider": "finam_rss",
            "error": str(exc)[:300],
            "symbols": symbols,
            "verdict": "UNAVAILABLE",
        }

    headlines = _parse_rss_headlines(raw)
    if not headlines:
        return {
            "status": "failed",
            "provider": "finam_rss",
            "error": "rss feed did not contain headlines",
            "symbols": symbols,
            "verdict": "UNAVAILABLE",
        }

    matched = _matching_headlines(symbols, headlines)
    if not matched:
        summary = "Finam RSS: свежих совпадений по тикерам не найдено."
        return {
            "status": "facts",
            "provider": "finam_rss",
            "symbols": symbols,
            "summary": summary,
            "verdict": "UNAVAILABLE",
            "provider_call": False,
            "rss_flags": [],
        }

    negative_markers = (
        "санкц",
        "суд",
        "иск",
        "убыт",
        "паден",
        "сниж",
        "дивидендн",
        "гэп",
        "дефолт",
        "риск",
        "штраф",
        "расслед",
    )
    summary = "Finam RSS: " + "; ".join(matched[:8])
    flags = [item for item in matched if any(marker in item.lower() for marker in negative_markers)]
    return {
        "status": "facts",
        "provider": "finam_rss",
        "symbols": symbols,
        "summary": summary,
        "verdict": "UNAVAILABLE",
        "provider_call": False,
        "rss_flags": (flags or matched)[:5],
    }


def _read_h4_cache(
    symbols: list[str],
    config: ResearchConfig,
    *,
    cache_root: Path,
    mode: str,
    now: datetime | None,
) -> dict[str, Any] | None:
    if mode != "h4_news_gate":
        return None
    path = _h4_cache_path(symbols, config, cache_root=cache_root, mode=mode)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    now_utc = now or datetime.now(timezone.utc)
    expires_at = _parse_iso_datetime(payload.get("expires_at"))
    if expires_at is not None and expires_at < now_utc:
        return None
    if payload.get("status") != "ok":
        return None
    payload["cache"] = {"status": "hit", "path": str(path), "expires_at": payload.get("expires_at")}
    payload["cache_hit"] = True
    payload["provider_call"] = False
    payload["_artifact_path"] = str(path)
    return payload


def _write_h4_cache(
    payload: dict[str, Any],
    symbols: list[str],
    config: ResearchConfig,
    *,
    cache_root: Path,
    mode: str,
    now: datetime | None,
) -> None:
    if mode != "h4_news_gate" or payload.get("status") != "ok":
        return
    now_utc = now or datetime.now(timezone.utc)
    expires_at = _iso_utc(now_utc + timedelta(seconds=config.h4_cache_ttl_seconds))
    path = _h4_cache_path(symbols, config, cache_root=cache_root, mode=mode)
    stored = {**payload, "expires_at": expires_at, "cache": {"status": "miss", "stored": True, "path": str(path), "expires_at": expires_at}}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return
    payload["expires_at"] = expires_at
    payload["cache"] = stored["cache"]
    payload["cache_hit"] = False


def _h4_cache_path(symbols: list[str], config: ResearchConfig, *, cache_root: Path, mode: str) -> Path:
    model = _model_for_mode(config, mode)
    prompt = _prompt_for_mode(symbols, mode)
    key_payload = {
        "mode": mode,
        "symbols": sorted(symbols),
        "model": model,
        "max_tokens": _max_tokens_for_mode(config, mode),
        "finam_rss_enabled": config.finam_rss_enabled,
        "finam_rss_url": config.finam_rss_url,
        "prompt": prompt,
    }
    digest = sha256(json.dumps(key_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return cache_root / "h4_gate" / f"{digest}.json"


def _usage_payload(data: dict[str, Any], *, prompt: str, summary: str) -> dict[str, Any]:
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict):
        return {
            "source": "provider",
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "prompt_chars": len(prompt),
            "completion_chars": len(summary),
        }
    return {
        "source": "estimated_chars",
        "prompt_chars": len(prompt),
        "completion_chars": len(summary),
        "estimated_total_tokens": max(1, (len(prompt) + len(summary) + 3) // 4),
    }


def _provider_failure_payload(
    exc: Exception,
    *,
    provider: str,
    mode: str,
    symbols: list[str],
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    error = _sanitized_provider_error(exc)
    payload = {
        "status": "failed",
        "provider": provider,
        "mode": mode,
        "model": model,
        "requested_max_tokens": max_tokens,
        "classification": "provider_client_failure",
        "error": error,
        "symbols": symbols,
        "verdict": "UNAVAILABLE",
    }
    if _looks_like_provider_client_typeerror(exc):
        payload["reason"] = "provider_client_typeerror"
    return payload


def _sanitized_provider_error(exc: Exception) -> str:
    raw = str(exc)
    if _looks_like_provider_client_typeerror(exc):
        return "Provider client failed before producing a response; no trading signal was generated."
    return raw[:300]


def _looks_like_provider_client_typeerror(exc: Exception) -> bool:
    text = str(exc)
    return isinstance(exc, TypeError) and "NoneType" in text and "iterable" in text


def _parse_rss_headlines(raw: str) -> list[str]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    headlines: list[str] = []
    for item in root.findall(".//item"):
        title = item.findtext("title")
        description = item.findtext("description")
        text = " ".join(part.strip() for part in (title or "", description or "") if part and part.strip())
        if text:
            cleaned = re.sub(r"<[^>]+>", "", unescape(text))
            headlines.append(re.sub(r"\s+", " ", cleaned).strip())
    return headlines


def _matching_headlines(symbols: list[str], headlines: list[str]) -> list[str]:
    tickers = {symbol.split("@", 1)[0].upper() for symbol in symbols}
    matches: list[str] = []
    for headline in headlines:
        upper = headline.upper()
        if any(ticker in upper for ticker in tickers):
            matches.append(headline)
    return matches


def _prompt_for_mode(symbols: list[str], mode: str) -> str:
    joined = ", ".join(symbols)
    if mode == "h4_news_gate":
        return (
            "Ты проверяешь только material news/corporate blockers для возможной LONG-сделки по MOEX.\n"
            f"Tickers: {joined}\n"
            "Окно: новости за последние 7 календарных дней; события ближайших 14 дней.\n"
            "Проверять только: дивидендная отсечка/гэп-риск; санкции; регуляторные ограничения; "
            "судебные/корпоративные риски; отчётность/guidance, явно ухудшающие риск; остановка торгов; "
            "очевидная причина НЕ открывать LONG сейчас.\n"
            "Не анализируй технику, тренд, мультипликаторы, прогноз цены и общие новости рынка. "
            "Если material blocker не найден — OK.\n"
            'Ответ строго JSON без markdown: {"items":[{"symbol":"SBER@MISX","verdict":"OK|RISK|AVOID",'
            '"reason":"до 120 символов","source_hint":"до 80 символов"}],"overall_verdict":"OK|RISK|AVOID"}'
        )
    if mode == "daily_digest":
        return (
            "Сделай короткий daily research digest для MOEX universe на сегодня.\n"
            f"Tickers: {joined}\n"
            "Фокус: важные события дня, секторные риски, позитивный/негативный фон, чего не трогать. "
            "Не делай торговых рекомендаций и прогнозов цены.\n"
            "Ответ строго JSON без markdown: "
            '{"market_regime":"positive|neutral|risk",'
            '"sector_notes":[{"sector":"banks","bias":"positive|neutral|risk","reason":"до 140 символов"}],'
            '"positive_context":["до 120 символов"],"risk_context":["до 120 символов"],'
            '"avoid_symbols":["GAZP@MISX"],"priority_watchlist":["SBER@MISX"],'
            '"event_watchlist":[{"symbol":"TATN@MISX","event":"до 100 символов","date":"YYYY-MM-DD|null","risk":"до 100 символов"}]}'
        )
    if mode == "weekly_review":
        return (
            "Сделай weekly universe review для MOEX на следующую торговую неделю.\n"
            f"Tickers: {joined}\n"
            "Фокус: сектора, события недели, бумаги с повышенным риском, бумаги для приоритетного наблюдения. "
            "Не меняй policy и не давай приказов на сделки.\n"
            "Ответ строго JSON без markdown: "
            '{"market_regime":"positive|neutral|risk",'
            '"sector_bias":[{"sector":"oil_gas","bias":"positive|neutral|risk","reason":"до 140 символов"}],'
            '"avoid_symbols":["GAZP@MISX"],"priority_watchlist":["SBER@MISX"],'
            '"event_watchlist":[{"symbol":"TATN@MISX","event":"до 100 символов","date":"YYYY-MM-DD|null","risk":"до 100 символов"}],'
            '"policy_suggestions":[{"action":"add_to_watchlist|reduce_priority|exclude_temporarily","symbol":"SBER@MISX","reason":"до 120 символов"}]}'
        )
    return (
        "Сделай deep pre-trade news/corporate check перед возможной LONG-сделкой.\n"
        f"Symbol: {joined}\n"
        "Фокус: есть ли причина НЕ входить прямо сейчас. Проверяй новости 14 дней и события 30 дней. "
        "Не анализируй технику и не прогнозируй цену.\n"
        "Ответ строго JSON без markdown: "
        '{"symbol":"SBER@MISX","verdict":"OK|RISK|AVOID",'
        '"blockers":["до 120 символов"],"near_term_events":["до 120 символов"],'
        '"reason_not_to_enter":"до 160 символов или пусто","confidence":"low|medium|high"}'
    )


def _model_for_mode(config: ResearchConfig, mode: str) -> str:
    if mode == "h4_news_gate":
        return config.h4_model
    if mode == "daily_digest":
        return config.daily_model
    if mode == "weekly_review":
        return config.weekly_model
    if mode == "pretrade_check":
        return config.pretrade_model
    return config.model


def _max_tokens_for_mode(config: ResearchConfig, mode: str) -> int:
    if mode == "h4_news_gate":
        return config.h4_max_tokens
    if mode == "daily_digest":
        return config.daily_max_tokens
    if mode == "weekly_review":
        return config.weekly_max_tokens
    return config.pretrade_max_tokens


def _parse_structured_content(content: str, symbols: list[str], mode: str) -> dict[str, Any] | None:
    raw = _strip_code_fence(content.strip())
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if mode == "h4_news_gate":
        items = parsed.get("items")
        if not isinstance(items, list):
            return None
        clean_items: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "")
            if symbol not in symbols:
                continue
            verdict = _clean_verdict(item.get("verdict"))
            clean_items.append(
                {
                    "symbol": symbol,
                    "verdict": verdict,
                    "reason": str(item.get("reason") or "")[:180],
                    "source_hint": str(item.get("source_hint") or "")[:120],
                }
            )
        if not clean_items:
            return None
        overall = _overall_verdict([item["verdict"] for item in clean_items])
        return {"items": clean_items, "verdict": overall, "overall_verdict": overall}
    if mode == "pretrade_check":
        verdict = _clean_verdict(parsed.get("verdict"))
        return {**parsed, "verdict": verdict}
    return {**parsed, "verdict": _digest_verdict(parsed)}


def _strip_code_fence(value: str) -> str:
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _clean_verdict(value: Any) -> str:
    verdict = str(value or "UNAVAILABLE").upper()
    return verdict if verdict in VERDICTS else "UNAVAILABLE"


def _overall_verdict(verdicts: list[str]) -> str:
    if "AVOID" in verdicts:
        return "AVOID"
    if "RISK" in verdicts:
        return "RISK"
    if "OK" in verdicts:
        return "OK"
    return "UNAVAILABLE"


def _digest_verdict(payload: dict[str, Any]) -> str:
    avoid = payload.get("avoid_symbols")
    risk_context = payload.get("risk_context")
    if isinstance(avoid, list) and avoid:
        return "RISK"
    if isinstance(risk_context, list) and risk_context:
        return "RISK"
    return "OK"


def _artifact_markdown(kind: str, payload: dict[str, Any]) -> str:
    title = {
        "daily": "Daily Research Digest",
        "weekly": "Weekly Research Review",
        "pretrade": "Pre-trade Research Check",
    }[kind]
    lines = [f"# {title}", ""]
    for key, value in payload.items():
        if key.startswith("_"):
            continue
        lines.append(f"- **{key}**: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _iso_week(value: datetime) -> str:
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
