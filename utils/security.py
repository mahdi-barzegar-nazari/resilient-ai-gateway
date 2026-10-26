"""Access control and secret hygiene."""
import config


def is_authorized(user_id):
    """Telegram: an empty allow-list means the bot is open to everyone."""
    if not config.ALLOWED_USERS:
        return True
    return user_id in config.ALLOWED_USERS


def is_authorized_bale(user_id, chat_id):
    """Bale: a message is accepted if either the sender or the chat is allow-listed."""
    if not config.ALLOWED_USERS:
        return True
    return user_id in config.ALLOWED_USERS or chat_id in config.ALLOWED_USERS


def _secrets():
    return (
        config.TELEGRAM_BOT_TOKEN,
        config.BALE_BOT_TOKEN,
        config.GROQ_API_KEY,
        config.GEMINI_API_KEY,
        config.TAVILY_API_KEY,
        # every key of the *_API_KEYS lists (rotation), not only the singular variables
        *config.api_keys("groq"),
        *config.api_keys("gemini"),
        *config.api_keys("tavily"),
    )


def redact_secrets(value):
    """Replace API keys and bot tokens with '***'.

    Needed because file-download URLs (and therefore many exception messages) embed
    the bot token.
    """
    text = str(value)
    # Longest first: if one key is a prefix of another, masking the short one first would leave a
    # readable tail of the long one behind.
    for secret in sorted(set(filter(None, _secrets())), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def short_err(error):
    """Compact, redacted one-line description of an exception: 'TypeName: message[:200]'."""
    return redact_secrets(f"{type(error).__name__}: {str(error)[:200]}")
