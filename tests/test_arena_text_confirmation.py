import importlib.util
import json
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "arena_text_confirmation.py"
spec = importlib.util.spec_from_file_location("arena_text_confirmation", MODULE_PATH)
arena = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = arena
spec.loader.exec_module(arena)


class Completed:
    returncode = 0
    stdout = "ok"
    stderr = ""


def test_authorized_confirm_invokes_arena_confirm_live(tmp_path):
    env_file = tmp_path / "runtime.env"
    env_file.write_text("ARENA_TOKEN=secret\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    result = arena.handle_arena_confirmation_text(
        "CONFIRM_ARENA_BUY SBER@MISX DEMO-RU",
        "42",
        {"42"},
        event_log=tmp_path / "events.jsonl",
        env_files=(env_file,),
        runner=runner,
    )
    assert result.handled is True
    assert result.authorized is True
    assert result.command[:2] == ["arena-confirm", "--live"]
    assert "--confirmation" in result.command
    assert calls[0][1]["env"]["ARENA_TOKEN"] == "secret"


def test_authorized_portfolio_confirmations_invoke_arena_confirm_live(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    replace = arena.handle_arena_confirmation_text(
        "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
        "42",
        {"42"},
        event_log=tmp_path / "events.jsonl",
        env_files=(),
        runner=runner,
    )
    exit_result = arena.handle_arena_confirmation_text(
        "CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US",
        "42",
        {"42"},
        event_log=tmp_path / "events.jsonl",
        env_files=(),
        runner=runner,
    )

    assert replace.handled is True
    assert replace.authorized is True
    assert exit_result.handled is True
    assert exit_result.authorized is True
    assert calls[0][0] == [
        "arena-confirm",
        "--live",
        "--confirmation",
        "CONFIRM_ARENA_REPLACE SBER@MISX -> PLZL@MISX DEMO-RU",
    ]
    assert calls[1][0] == [
        "arena-confirm",
        "--live",
        "--confirmation",
        "CONFIRM_ARENA_EXIT NVDA@XNGS DEMO-US",
    ]


def test_authorized_override_confirmation_invokes_arena_run_live(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    result = arena.handle_arena_confirmation_text(
        "CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US",
        "42",
        {"42"},
        event_log=tmp_path / "events.jsonl",
        env_files=(),
        runner=runner,
    )

    assert result.handled is True
    assert result.authorized is True
    assert calls[0][0] == [
        "arena-run",
        "--account",
        "DEMO-US",
        "--live",
        "--confirmation",
        "CONFIRM_ARENA_BUY_OVERRIDE AVGO@XNGS DEMO-US",
    ]


def test_authorized_recover_confirmation_invokes_arena_recover_live(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    result = arena.handle_arena_confirmation_text(
        "CONFIRM_ARENA_RECOVER SBER@MISX DEMO-RU",
        "42",
        {"42"},
        event_log=tmp_path / "events.jsonl",
        env_files=(),
        runner=runner,
    )

    assert result.handled is True
    assert result.authorized is True
    assert calls[0][0] == [
        "arena-recover-protection",
        "--account",
        "DEMO-RU",
        "--symbol",
        "SBER@MISX",
        "--live",
        "--confirmation",
        "CONFIRM_ARENA_RECOVER SBER@MISX DEMO-RU",
    ]


def test_authorized_unsupported_arena_confirmation_is_logged_without_running(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    result = arena.handle_arena_confirmation_text(
        "CONFIRM_ARENA_UNKNOWN SBER@MISX DEMO-RU",
        "42",
        {"42"},
        event_log=tmp_path / "events.jsonl",
        env_files=(),
        runner=runner,
    )
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    assert result.handled is True
    assert result.authorized is True
    assert result.command == []
    assert result.returncode is None
    assert calls == []
    assert events[-1]["event"] == "unsupported"


def test_unauthorized_chat_cannot_execute(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return Completed()

    result = arena.handle_arena_confirmation_text(
        "CONFIRM_ARENA_SELL AAPL@XNGS DEMO-US",
        "99",
        {"42"},
        event_log=tmp_path / "events.jsonl",
        env_files=(),
        runner=runner,
    )
    assert result.handled is True
    assert result.authorized is False
    assert calls == []
