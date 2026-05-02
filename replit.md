# Workspace

## Overview

pnpm workspace monorepo using TypeScript + Python Telegram bot. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)
- **Python**: 3.11 (for Telegram bot)

## Telegram Forex Bot (`bot/`)

A modular Python Telegram bot for forex chart analysis. Located in `bot/` directory.

### Architecture

- `config.py` — Environment variables, plan limits, watch/entry alert thresholds, all new gate constants
- `storage.py` — JSON file storage with thread-safe read/write; rejection log, latency tracking, pre-alerts; SMC analytics fields
- `access.py` — Access code system, plan management, admin controls
- `vision.py` — OpenAI GPT-4o vision API for chart reading + quality scoring
- `market_data.py` — Live market data via Twelve Data API; `compute_mtf_alignment()` for D1/H4/H1 confluence
- `news_data.py` — Economic calendar (Finnhub / free fallback)
- `indicators.py` — EMA, ATR, swing high/low, market context; choppy regime detection
- `decision_engine.py` — Rule-based trade decisions combining vision + data + news; logs rejections; ATR SL floor 1.2x
- `scanner.py` — Auto-scanner with two-stage alerts; all Phase 2 gates; SMC gate; MTF scoring; improved narratives
- `structure_signals.py` — SMC detection layer: liquidity sweep, FVG, order block, breaker, Fib, volatility scoring
- `smc.py` — SMC validation gate (`validate_smc_setup`), narrative builder, analytics feature extractor
- `loss_streak.py` — Per-pair loss streak protection + global drawdown pause
- `circuit_breaker.py` — Confidence bump after losses (existing)
- `analytics.py` — All performance analytics computations (component, regime, session, latency, rejection)
- `outcome_checker.py` — Background thread resolving scanner alerts; tracks latency_minutes
- `sessions.py` — Session scoring, dead hours, session labels
- `formatters.py` — Telegram message formatting, lot size estimation
- `main.py` — Bot commands, photo handler, scheduler, health server

### Key Features

- Private access code system (trial/weekly/monthly/vip plans)
- Smart chart analysis: GPT-4o vision + live data + news filtering + indicators
- Chart quality scoring: 6-dimension GPT scoring, specific user feedback, confidence penalty
- Two-stage scanner: watch alerts (forming) + entry alerts (confirmed), separate dedup
- Score breakdown tracking on every alert for analytics
- Outcome tracking: TP/SL resolution with latency tracking
- Rejection logging: every meaningful rejected setup/chart is categorised and stored
- Admin analytics: /componentstats, /confidencecal, /regimestats, /pairsessionstats, /latencystats, /rejectionstats, /recentrejections
- Admin commands: /gencode, /revokeuser, /revokecode, /users, /codes, /auditalert, /expectancy
- User commands: /start, /help, /redeem, /myplan, /news, /lot, /watch, /win, /loss, /stats
- Scheduled alerts: morning brief, London/NY open, London close, news warnings
- Health server on PORT for deployment monitoring

### Config / Env Vars

- `WATCH_ALERT_MIN_CONFIDENCE` — Min confidence for watch (pre-alert), default 60
- `WATCH_ALERT_COOLDOWN_MINUTES` — Cooldown between watch alerts per pair, default 90
- `REJECTION_LOG_LIMIT` — Max rejection log entries, default 1000
- `ANALYTICS_SAMPLE_LIMIT` — Max alerts to sample for analytics, default 2000

### Required Secrets

- `TELEGRAM_BOT_TOKEN` — From @BotFather
- `OPENAI_API_KEY` — For chart analysis
- `ADMIN_IDS` — Comma-separated Telegram user IDs

### Optional Secrets

- `TWELVEDATA_API_KEY` — Live market quotes and candles
- `FINNHUB_API_KEY` — Economic calendar
- `FOREX_NEWS_API_KEY` — Alternative news source

### Running

- Workflow: `Forex Bot` — runs `cd bot && python main.py`
- Only one bot instance can poll Telegram at a time

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Recent Changes (Apr 2026)
- **Range Mode**: When both HTF and HTF2 are neutral, scanner switches to mean-reversion (target RR 1.2, floor 1.0) instead of rejecting as bad regime. Setup carries `range_mode: true`.
- **Stale Quote Fallback**: If live quote is missing/stale, scanner falls back to last candle close instead of dropping the setup. Setup carries `quote_fallback: true`.
- **Missed Winners Tracker**: Setups rejected for low_confluence (had valid entry/SL/TP) are recorded as virtual trades. Outcome checker resolves them on the same schedule as real alerts. Use `/missedwinners [hours]` (admin) to see hypothetical win rate by rejection reason.

