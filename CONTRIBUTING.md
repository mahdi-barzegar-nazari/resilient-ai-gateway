# Contributing

Bug reports and pull requests are welcome. This is a solo, spare-time project, so replies may take a few days.

## Set up

**Prerequisites:** Python 3.10 or newer and Git.

```bash
git clone https://github.com/mahdi-barzegar-nazari/resilient-ai-gateway.git
cd resilient-ai-gateway

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m unittest discover -s tests -t . -v
```

The tests need **no network and no API keys**. To run the bot itself, copy `.env.example` to `.env` and fill in your own tokens.

## Ground rules

- **Adapters stay thin.** `bot/` receives updates and renders replies. Decisions belong in `core/` and `services/`, so a fix lands on Telegram and Bale together.
- **Services never import `bot/`.** See [`docs/architecture.md`](./docs/architecture.md) for the dependency rule.
- **Read settings as `config.NAME`**, never `from config import NAME`, so tests can patch them.
- **No real network calls in tests, and no sleeping.** Mock the call; a stub that is reached unmocked raises on purpose. Time-dependent code takes an injectable clock: use `tests.helpers.FakeClock` and advance it instead of waiting.
- **Never log or display a secret.** Pass user-visible errors through `redact_secrets`. A new API key, and every key of its `*_API_KEYS` list, must be added to `_secrets()` in `utils/security.py`; `config.api_keys(provider)` already covers Groq, Gemini and Tavily.
- **Keyed provider calls go through the key pool.** Read keys with `config.api_keys(...)` and run the call through `services/key_pool.py` (see the *API key rotation* section of the architecture notes). Log a key only as provider and position, never any part of it, and never sleep or back off inside rotation.
- **Fix a bug with a regression test** that fails without the fix.

## Pull request checklist

- [ ] `python -m unittest discover -s tests -t .` passes
- [ ] New behaviour has a test, and a bug fix has a regression test
- [ ] No secrets, tokens or personal IDs in the diff
- [ ] `README.md` and `README.fa.md` are both updated when commands, configuration or behaviour change
- [ ] The test count (both READMEs, `CHANGELOG.md` and the table in `docs/architecture.md`) is updated if it changed
- [ ] `CHANGELOG.md` has an entry under **Unreleased**

## Security issues

Please do not open a public issue for a vulnerability. Follow [`SECURITY.md`](./SECURITY.md).
