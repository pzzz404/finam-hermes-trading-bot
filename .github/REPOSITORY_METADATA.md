# GitHub publication texts

Готовые тексты для создания публичного репозитория. Основной вариант описания
на английском выбран для поиска и международной аудитории; README остаётся
русскоязычным с английским summary.

## Repository settings

- Name: `finam-hermes-trading-bot`
- Visibility: `Public`
- Website: `https://www.finam.ru/publications/item/bitva-algoritmov-zachem-treydery-testiruyut-ii-na-finam-arene-20260627-2040/`
- About description:

  > Герман (Herman) is an AI trading agent built on Hermes and tested for two months at Finam Arena with risk controls, learning loops, and guarded execution.

- Русская альтернатива для About:

  > Герман — торговый ИИ-агент на платформе Hermes и публичный артефакт двухмесячного эксперимента на конкурсе «Финам Арена».

## Topics

```text
ai-agents
algorithmic-trading
finam
finam-api
fintech
hermes
market-data
paper-trading
python
quantitative-finance
risk-management
systemd
telegram-bot
trading-bot
```

## Short tagline

> Герман — торговый ИИ-агент на Hermes. Built in the Arena. Paper by default.

## Repository introduction

### Русский вариант

> Этот репозиторий вырос из двухмесячного эксперимента на конкурсе «Финам
> Арена» 2026 года. Торгового ИИ-агента зовут Герман, а Hermes — платформа, на
> которой он был собран. Герман торговал виртуальным капиталом
> по реальным рыночным котировкам, а я шаг за шагом строил вокруг него систему,
> которая должна была не столько находить сделки, сколько не давать агенту
> действовать без проверки: рыночный сканер, торговая гипотеза, риск-контроль,
> защищённое исполнение, журнал ошибок и человеческий надзор. Здесь опубликован
> очищенный технический артефакт этих двух месяцев — без реальных счетов,
> токенов, торговой истории и обещаний доходности.

### English version

> This repository grew out of a two-month experiment during the 2026 Finam
> Arena algorithmic-trading competition. The trading agent is called Герман
> (Herman), and Hermes is the platform on which it was built. Герман operated
> virtual portfolios against real market quotes while the surrounding system
> evolved around a deliberately unglamorous goal: prevent unchecked action.
> Market scanning, trade hypotheses, risk controls, guarded execution, error
> journals and human oversight are preserved here as a sanitized engineering
> artifact—not as a claim of a profitable autonomous strategy.

## Initial release

- Tag: `v0.1.0`
- Title: `v0.1.0 — Initial public release`
- Release notes:

```markdown
Initial public release of Герман — Finam Hermes Trading Bot, an unofficial experimental
artifact of a two-month AI-trading experiment conducted during the 2026 Finam
Arena competition. Герман (Herman) is the trading AI agent; Hermes is the
platform on which it was built. During the Arena, Герман operated
virtual portfolios on real market quotes through Finam Trade API.

The point of the experiment was not to build a “magic neural network.” It was
to learn how to surround an agent with market scanning, explicit trade
hypotheses, portfolio risk controls, guarded execution, post-trade learning and
human oversight.

Highlights:

- deterministic offline paper-trading demo;
- paper mode enabled by default;
- independent gates for every live broker mutation;
- anonymized and paused H4/Arena example policies;
- optional Hermes, Telegram, and systemd adapters;
- centralized redaction for credentials and account identifiers;
- offline test suite, package build, Gitleaks, dependency audit, and CodeQL CI.

This release contains no credentials, real account identifiers, trade history,
runtime logs, or private Git history.

Background and author interview:
https://www.finam.ru/publications/item/bitva-algoritmov-zachem-treydery-testiruyut-ii-na-finam-arene-20260627-2040/

This project is not affiliated with Finam and is not investment advice. Live
trading can result in substantial or total loss. Review the code, policies, API
behavior, and broker restrictions independently before opening any live gate.
```

## Repository feature settings after publication

Enable Issues only if feedback will be monitored. Keep Wiki and Discussions
disabled initially. Enable Private Vulnerability Reporting, secret scanning,
push protection, Dependabot alerts and security updates, and CodeQL.
