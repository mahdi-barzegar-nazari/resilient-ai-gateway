# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

First public release.

### Added

- Telegram (pyTelegramBotAPI) and Bale (hand-written polling client) adapters on one shared, platform-agnostic pipeline.
- Automatic Groq ⇄ Gemini failover on rate limits, timeouts and empty answers, with a short note appended to the reply.
- Voice notes (Whisper), image and PDF understanding (Gemini), link summaries and live web context (Tavily, with a Google News RSS fallback).
- Student toolbox: `/debug`, `/read`, `/wiki`, `/arxiv`, `/arxiv_full`, and an adaptive multiple-choice `/quiz` with a strict JSON contract.
- Exam mode, long-output mode, and opt-in conversation memory (off by default).
- Allow-list access control (`ALLOWED_USERS`) across commands, text, buttons and media.
- Per-user rate limiting, shared by Telegram and Bale and on by default: at most `RATE_LIMIT_MAX` (20) AI-consuming requests per `RATE_LIMIT_WINDOW_SECONDS` (60) for each `(platform, user id)`; `RATE_LIMIT_MAX=0` turns it off. Free text, voice, photos, PDFs and every command or button that calls Groq, Gemini or Tavily are counted, while `/start`, the panel, settings, `/clear`, quiz answers, `/quiz_stop` and the keyless `/wiki` and `/arxiv` are not. The check runs right after the allow-list and before any download or provider call. An over-limit user gets one short Persian notice per window with the seconds to wait, and further messages in that window are dropped silently. The decision logic lives in `core/ratelimit.py` (sliding window, in memory, thread-safe, injectable clock, stale entries swept out).
- Secret redaction for user-visible errors and for every log line, tracebacks included.
- Multi-key rotation for Groq, Gemini and Tavily. `GROQ_API_KEYS`, `GEMINI_API_KEYS` and `TAVILY_API_KEYS` take comma-separated keys and are merged with the existing singular variables; requests rotate round-robin and a key is skipped while it cools down. A 429/quota (capped `Retry-After`), timeout, connection error, 5xx or empty answer retries the same request on the next key, a 401/403 benches the key for a long time, and a 400-class request error is never rotated. If every key of a provider is cooling down the call fails immediately, so the Groq ⇄ Gemini failover is unchanged. Covers Groq chat and Whisper, Gemini text, image and PDF (including the long-timeout client), `/arxiv_full`, and Tavily search and extract. One key behaves exactly as before.
- Tunables `KEY_MAX_ATTEMPTS`, `KEY_RETRY_BUDGET_SECONDS`, `KEY_COOLDOWN_RATE_LIMIT`, `KEY_COOLDOWN_RATE_LIMIT_MAX`, `KEY_COOLDOWN_TRANSIENT` and `KEY_COOLDOWN_AUTH`, defined in `config.py` and overridable through environment variables of the same name.
- Startup log line with the number of loaded keys per provider (never the keys).
- Smart web search. A small LLM "search planner" (`services/search_planner.py`) decides whether a message needs fresh web data and writes the query from the last few messages of history, so a follow-up such as "و یورو؟" becomes a complete query. It is one cheap call: the fast Groq model (Gemini when Groq is missing or its call fails), temperature 0, a tiny prompt, a short timeout, and strict JSON `{"search": bool, "query": str}`. The answer is parsed defensively (`<think>` blocks and code fences stripped, types validated). A timeout, an error, an empty answer, invalid JSON or `SEARCH_PLANNER_ENABLED=false` falls back to the old keyword heuristic, so the planner can never block an answer.
- Planner skip rules in `services/web_service.py`: greetings and very short messages never search, and explicit search phrases ("سرچ کن", "search", ...) always search, both without a model call.
- Bounded, thread-safe TTL cache for identical search queries (`web_service.TTLCache`, injectable clock). `/web` and `/testweb` neither read nor write it.
- Source links: up to three links of the web results are appended under the answer, labelled «منابع:». They are percent-encoded plain text, so they render correctly on Telegram (HTML) and Bale, and they never appear in exam mode or in conversation memory.
- Tunables `SEARCH_PLANNER_ENABLED`, `SEARCH_PLANNER_TIMEOUT_SECONDS` and `SEARCH_CACHE_TTL_SECONDS`, defined in `config.py` and overridable through environment variables of the same name.
- Health-check server, `Dockerfile`, and a Render Blueprint (`render.yaml`).
- 612 offline unit tests and a GitHub Actions workflow running them on Python 3.10 to 3.12.
- English and Persian READMEs, architecture notes, contributing guide, and security policy.
- Ruff lint gate: `ruff check .` with the `E`, `F`, `W` and `B` rule families, settings in `pyproject.toml` (no formatter), Ruff pinned in `requirements-dev.txt`, and a separate `lint` job in CI.

### Changed

- Live web context now reaches whichever provider answers: it is added to the prompt on the Groq-first and the Gemini-first path and reused on failover, instead of only on the Gemini path. A request makes at most one search. Exam mode, link-summary prompts, images and PDFs still never search, and `/web` is unchanged.
- The planner call belongs to the user request that triggered it: it uses the normal Groq and Gemini key rotation and is not counted again by the per-user rate limiter.
- `redact_secrets` masks every key of every `*_API_KEYS` list and replaces the longest secret first, so a key that is a prefix of another leaves no readable tail.
- The startup warning about missing AI keys now also counts keys given only through `GROQ_API_KEYS` / `GEMINI_API_KEYS`.

