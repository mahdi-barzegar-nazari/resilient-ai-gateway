# Security Policy

## Supported versions

Only the latest release on `main` receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Use one of these private channels:

- GitHub: **Security → Report a vulnerability** on this repository (private vulnerability reporting).
- Email: [m.barzegar.nazari@gmail.com](mailto:m.barzegar.nazari@gmail.com)

Include what you found, how to reproduce it, and the impact you expect. I will reply as soon as I can. This is a solo, spare-time project, so please allow time for a fix.

## In scope

- Leaking a bot token or API key through logs, error messages or chat replies
- Bypassing the `ALLOWED_USERS` allow-list (commands, text, buttons, voice, photos or documents)
- Prompt injection through fetched page content that changes the bot's behaviour
- Resource-exhaustion paths that bypass the size and duration limits

## If a secret was exposed

Revoke and regenerate it at the provider right away: BotFather (Telegram or Bale), Groq, Google AI Studio, or Tavily. Deleting a commit is not enough, because the old value stays in the Git history.

## Deployment checklist

- Set `ALLOWED_USERS` on every public deployment; each message consumes provider quota.
- Keep `.env` out of version control (it is in `.gitignore`).
- Do not add tools that act on the outside world without further hardening. Page text is treated as untrusted data, but it is still model input.
