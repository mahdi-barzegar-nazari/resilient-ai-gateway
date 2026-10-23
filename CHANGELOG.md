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
- Secret redaction for user-visible errors and for every log line, tracebacks included.
- Health-check server, `Dockerfile`, and a Render Blueprint (`render.yaml`).
- 248 offline unit tests and a GitHub Actions workflow running them on Python 3.10 to 3.12.
- English and Persian READMEs, architecture notes, contributing guide, and security policy.

