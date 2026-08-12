# Repository Instructions

## Architecture

- `src/finam_trading_bot/` содержит API-клиент, риск-контур, paper broker,
  H4/Arena и общие safety/redaction helpers.
- `scripts/hermes_operator.py` — опциональный Hermes/operator adapter.
- `config/` содержит только безопасные demo-политики.
- `data/runtime/` предназначен только для локального состояния и не коммитится.

## Commands

```bash
python -m pip install -e '.[dev,charts]'
python -m finam_trading_bot doctor --json
python -m finam_trading_bot paper-demo --json
ruff check .
pytest --disable-socket -q
python -m build
```

## Safety rules

- Не выполнять live API-вызовы и не отправлять заявки в тестах.
- `TRADING_MODE=paper` является обязательным значением по умолчанию.
- Не ослаблять одновременные live-gates, confirmation phrases и stop checks.
- Не добавлять секреты, реальные account/chat/portfolio ID, сделки, логи или
  host-specific пути в код, тесты, документацию и CI.
- Не менять торговые сигналы, коэффициенты, размеры позиций и риск-пороги без
  отдельного задания.
- Любые внешние API в тестах заменять fake/mock; тесты должны проходить с
  полностью запрещённой сетью.
