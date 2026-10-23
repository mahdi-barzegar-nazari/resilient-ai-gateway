# Architecture

How the gateway is built, why it is built that way, and where to change it. For installation and usage see the [README](../README.md).

## Design goals

1. **A provider outage must not look like an outage.** The same request is replayed on the other provider, transparently.
2. **One behaviour, two messengers.** Adapters stay thin; every decision lives in a shared pipeline.
3. **Safe by default.** Conversation memory is opt-in, access can be restricted to an allow-list, and secrets are scrubbed from errors and logs.
4. **Testable offline.** No test needs a network connection, an API key, or a real SDK.

## Layers and the dependency rule

| Layer | Package | Rule |
| :--- | :--- | :--- |
| Adapters | `bot/` | Talk to one messenger. Check the allow-list, download media, render replies. **No decision logic.** |
| Core | `core/` | `Session` state and the shared `run_chat` pipeline. |
| Services | `services/` | Provider routing, web, audio, media, knowledge, quiz, tools. **Never import `bot/`.** |
| Utils | `utils/` | Pure helpers with no knowledge of the layers above. |
| Config | `config.py` | Read as `config.NAME` at call time, never `from config import NAME`, so tests can patch a value. |

The direction is `bot → core → services → utils / config`. The only edge back is `services → core.prompts` and `core.errors`, which are leaf modules (they import nothing from the project), so there are no import cycles. Adding a third messenger therefore means writing one new adapter and touching nothing else.

## Process and thread model

`main.py` starts up to three loops in one process:

| Loop | Thread | Started when |
| :--- | :--- | :--- |
| Health server (Flask, `/` and `/healthz`) | daemon | always |
| Bale long polling (hand-written client) | daemon | `BALE_BOT_TOKEN` is set |
| Telegram long polling (pyTelegramBotAPI) | **main** | `TELEGRAM_BOT_TOKEN` is set |

If Telegram is not configured, Bale runs on the main thread instead. With neither token the process logs an error and exits with status 1. The health server exists because free hosting tiers expect a web service listening on `$PORT`.

## Request lifecycle

The flowchart in the [README](../README.md#architecture) shows the full routing. The core of it is `core.pipeline.run_chat(session, user_text, image=None, memory_text=None)`:

1. Unless the chat is in exam mode or the input is media, links in the text are fetched and turned into a bounded prompt (`prepare_link_prompt`). Live web search is then switched off, because the page text is already in the prompt.
2. `process_ai_query` routes the request (see below).
3. The exchange is stored only when memory is on **and** exam mode is off. For links and media a short note is stored instead of the large prompt.
4. The function never raises: any failure becomes readable, secret-free text.

## Provider routing and failover

```mermaid
sequenceDiagram
    participant U as User
    participant A as Adapter
    participant P as Pipeline
    participant R as AI Router
    participant G as Groq
    participant M as Gemini

    U->>A: message
    A->>P: run_chat(session, text)
    P->>R: process_ai_query(...)
    R->>G: chat completion (no SDK retries)
    G--xR: 429 or timeout
    R->>M: same prompt (+ live web context if needed)
    M-->>R: answer
    R-->>P: answer + failover note
    P-->>A: reply text
    A-->>U: HTML-safe, chunked message(s)
```

Routing rules, all in `services/ai_router.py`:

- **Images and PDFs always go to Gemini.** It is the only multimodal provider, so there is no failover path for them.
- **The user's selected model decides who is tried first.** `/gemini` (or a missing Groq key) means Gemini first; otherwise Groq first.
- **Any failure replays the same request on the other provider.** That includes HTTP 429, timeouts and *empty* answers. The reply gets a short note saying the other provider answered.
- **Exam mode suppresses the note**, because the answer-sheet line must stay machine-readable.
- **Live web context is added only on the Gemini path.**
- **If both providers fail**, the raised error names both causes (secrets redacted).
- **Groq fails fast.** When a Gemini key exists the Groq client is built with `max_retries=0`, so failover does not wait for SDK back-off. PDFs use a separate Gemini client with a 180 s timeout instead of 60 s.

## State model

State is one `Session` dataclass per `(platform, chat_id)`, held in a thread-safe `SessionStore`:

| Field | Meaning |
| :--- | :--- |
| `model` | Selected engine (`/fast`, `/smart`, `/gemini`) |
| `memory_on` | Conversation memory; **off by default** |
| `exam_mode`, `long_mode` | Output modes |
| `history` | Last `MAX_HISTORY_MESSAGES` messages (10) while memory is on |
| `quiz`, `quiz_stats` | The question awaiting an answer, and the running score |
| `lock` | Makes quiz grading atomic, so a double tap is scored once |

The key includes the platform, so chat id `42` on Telegram and on Bale are different sessions. Everything is in memory and resets on restart (see the roadmap in the README).

## Errors and secrets

- **Allow-list everywhere.** Commands, free text, inline-keyboard presses, voice, photos and documents all pass `is_authorized` (Telegram) or `is_authorized_bale` (Bale: sender or chat) before anything is downloaded.
- **`redact_secrets`** replaces every configured token and key with `***`. Every user-visible error goes through it, because file-download URLs embed the bot token.
- **`RedactingFormatter`** scrubs the *final* log line, tracebacks included. The `urllib3` logger is raised to `WARNING` because its `DEBUG` lines print full URLs.
- **Page text is data, not instructions.** The link-summary prompt tells the model to ignore any instruction found inside the page text, and long pasted text that merely contains a link is not treated as a link request.

## Testing strategy

The suite uses the standard library only (`unittest`) and runs in about 0.13 s.

- **Config is patched, not imported by value.** Tests use `mock.patch.object(config, "NAME", value)`, which is why modules read `config.NAME` at call time.
- **SDKs are stubbed only when missing.** `tests/stubs.py` installs lightweight stand-ins if `groq`, `telebot` and the like are not installed. With `pip install -r requirements.txt` the real libraries are used.
- **Stubs fail loudly.** Every network-facing stub method raises `AssertionError`, so a test that forgets to mock a call fails instead of hanging.
- **Regression tests are named after the bug.** For example `VoiceDurationTests` (Bale reports milliseconds) and `PollingTests` (poisoned updates).

| Test file | Tests | Focus |
| :--- | :---: | :--- |
| `test_telegram_handler.py` | 52 | Wiring, panel, reply rendering with plain-text fallback, documents, voice, photos |
| `test_bale_handler.py` | 41 | Routing, voice duration units, media, transport, polling loop |
| `test_ai_router.py` | 28 | Failover, multimodal routing, prompt assembly, web enrichment |
| `test_web_service.py` | 28 | Search heuristics, URL extraction, link prompt budget, `/read` |
| `test_quiz_and_tools.py` | 20 | Quiz JSON contract and flow, tool registry |
| `test_knowledge_service.py` | 19 | Wikipedia, ArXiv ids, `/arxiv`, `/arxiv_full` |
| `test_formatters.py` | 14 | Message splitting, Markdown to Telegram HTML, truncation |
| `test_media_and_audio.py` | 12 | Image and PDF helpers, transcription |
| `test_pipeline.py` | 11 | `run_chat`, link handling, memory rule |
| `test_session.py` | 10 | `Session`, `SessionStore` |
| `test_security.py` | 8 | Authorization, redaction |
| `test_entrypoint.py` | 5 | Health server, `main()` wiring |
| **Total** | **248** | |

## Extending the gateway

**Add a command.** Write `tool_x(arg, model_name)` in `services/tools.py` (raise `ToolError` for user-facing problems) and add it to the `TOOLS` dict. Telegram registers `commands=list(TOOLS)` and Bale checks `cmd in TOOLS`, so both pick it up. Then add a row to the *Commands* table in both READMEs and a test.

**Add an AI provider.**
1. Add the key and model name to `config.py`, and the client to `services/clients.py` (`None` when the key is missing).
2. Add the key to `_secrets()` in `utils/security.py`, otherwise it would not be redacted.
3. Add a `call_<provider>` function that **raises on errors and on empty answers**, then extend the routing in `process_ai_query`.
4. Add tests next to `FailoverTests`.

**Add a messenger.** Create `bot/<name>_handler.py` with an app class: receive updates, call the right `is_authorized*` check, get a session with `sessions.get(PLATFORM, chat_id)`, then call `run_chat` or `run_tool` and render the reply within the platform's message limit. Start it from `main.py`. Nothing in `core/` or `services/` should need to change.

## Known trade-offs

Sessions live in memory, Bale updates are handled one at a time, web search is triggered by keyword heuristics, images and PDFs have no failover, and there is no per-user rate limit. Each has a planned next step in the README's [Trade-offs and Roadmap](../README.md#trade-offs-and-roadmap).
