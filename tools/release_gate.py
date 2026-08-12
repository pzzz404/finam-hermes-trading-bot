#!/usr/bin/env python3
"""Fail-closed structural gate for the clean public export.

The script reports paths and rule names only; it never prints matched values.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REQUIRED = {
    ".env.example",
    ".github/dependabot.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
    ".gitignore",
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "config/finam_arena_policy.json",
    "config/finam_h4_policy.json",
    "pyproject.toml",
    "src/finam_trading_bot/__main__.py",
    "src/finam_trading_bot/safety.py",
}
FORBIDDEN_PARTS = {".env", ".venv", "__pycache__", "data", "tmp", "backups", ".pytest_cache"}
FORBIDDEN_SUFFIXES = {".csv", ".db", ".jpeg", ".jpg", ".jsonl", ".log", ".pem", ".png", ".sqlite", ".zip"}
FORBIDDEN_TEXT_RULES = {
    "private_home_path": re.compile(re.escape("/home/" + "pzzz") + r"(?:/|\b)"),
    "private_account_id": re.compile(
        r"\b(?:" + "|".join(re.escape(item) for item in ("TRQD05:" + "217218", "1000" + "177", "1000" + "178", "1000" + "179")) + r")\b"
    ),
    "private_runtime_alias": re.compile(
        r"\b(?:" + "|".join(("ger" + "man", "va" + "lera", "bee" + "link", "be" + "get")) + r")\b",
        re.IGNORECASE,
    ),
    "private_proxy_port": re.compile(r"127\.0\.0\.1:31(?:2" + "8|2" + "9)\b"),
    "private_server_path": re.compile(r"/(?:opt/finam-hermes-trading-bot|etc/her" + "mes)(?:/|\b)"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_pat": re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
}


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    failures: list[dict[str, str]] = []
    files = sorted(
        path
        for path in root.rglob("*")
        if ".git" not in path.relative_to(root).parts and (path.is_file() or path.is_symlink())
    )
    relative = {path.relative_to(root).as_posix() for path in files}

    for missing in sorted(REQUIRED - relative):
        failures.append({"gate": "required_file", "path": missing})

    for path in files:
        rel = path.relative_to(root)
        rel_text = rel.as_posix()
        if path.is_symlink():
            failures.append({"gate": "symlink", "path": rel_text})
            continue
        if any(part in FORBIDDEN_PARTS for part in rel.parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append({"gate": "forbidden_artifact", "path": rel_text})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            failures.append({"gate": "non_text_file", "path": rel_text})
            continue
        for name, pattern in FORBIDDEN_TEXT_RULES.items():
            if pattern.search(text):
                failures.append({"gate": name, "path": rel_text})

    arena_path = root / "config" / "finam_arena_policy.json"
    if arena_path.is_file():
        try:
            arena = json.loads(arena_path.read_text(encoding="utf-8"))
            accounts = arena.get("accounts") if isinstance(arena, dict) else []
            safe = (
                arena.get("mode") == "approval"
                and arena.get("emergency_stop") is True
                and isinstance(accounts, list)
                and accounts
                and all(item.get("paused") is True and item.get("trade_mode") == "manual" for item in accounts)
            )
        except (OSError, json.JSONDecodeError):
            safe = False
        if not safe:
            failures.append({"gate": "safe_arena_policy", "path": "config/finam_arena_policy.json"})

    env_path = root / ".env.example"
    if env_path.is_file() and "TRADING_MODE=paper" not in env_path.read_text(encoding="utf-8"):
        failures.append({"gate": "paper_default", "path": ".env.example"})

    output = {"status": "PASS" if not failures else "FAIL", "files_checked": len(files), "failures": failures}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
