"""Central configuration: environment variables, model names and tunable limits.

Every other module reads values through ``config.NAME`` at call time (never
``from config import NAME``), so tests can override a value with
``mock.patch.object(config, "NAME", value)``.
"""
import math
import os


def _parse_user_ids(raw):
    return [int(uid.strip()) for uid in (raw or "").split(",") if uid.strip().isdigit()]


def parse_api_keys(*values):
    """Flatten comma-separated strings (or lists of them) into one de-duplicated key list.

    Spaces around a key and empty items are ignored. The first occurrence wins, so the order the
    operator wrote is the rotation order.
    """
    keys = []
    for value in values:
        items = value.split(",") if isinstance(value, str) else (value or [])
        for item in items:
            key = (item or "").strip()
            if key and key not in keys:
                keys.append(key)
    return keys


def _env_number(name, default, cast=float, minimum=0):
    """Numeric env var; anything missing, malformed, non-finite or below ``minimum`` gives ``default``."""
    try:
        value = cast(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= minimum else default


def _env_bool(name, default):
    """Boolean env var: 1/true/yes/on or 0/false/no/off in any case; anything else (or unset) gives ``default``."""
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


# ---------------------------------------------------------------- secrets / credentials
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

# Optional extra keys, comma-separated, merged with the singular variables above. Use api_keys()
# for the effective list; these three hold only what the *_API_KEYS variables contained.
GROQ_API_KEYS = parse_api_keys(os.environ.get("GROQ_API_KEYS"))
GEMINI_API_KEYS = parse_api_keys(os.environ.get("GEMINI_API_KEYS"))
TAVILY_API_KEYS = parse_api_keys(os.environ.get("TAVILY_API_KEYS"))


def api_keys(provider):
    """Effective keys of ``"groq"``, ``"gemini"`` or ``"tavily"``: the singular variable first, then the list.

    Evaluated at call time, so tests can patch either variable.
    """
    singular, plural = {
        "groq": (GROQ_API_KEY, GROQ_API_KEYS),
        "gemini": (GEMINI_API_KEY, GEMINI_API_KEYS),
        "tavily": (TAVILY_API_KEY, TAVILY_API_KEYS),
    }[provider]
    return parse_api_keys(singular, plural)


# Comma-separated numeric user IDs. Empty list = the bot is open to everyone.
ALLOWED_USERS = _parse_user_ids(os.environ.get("ALLOWED_USERS", ""))

# Port of the tiny health-check web server (Render, Railway, Fly... inject PORT).
PORT = int(os.environ.get("PORT", 8080))

# ---------------------------------------------------------------- models (overridable via env)
MODEL_FAST = os.environ.get("MODEL_FAST", "qwen/qwen3.8-27b")
MODEL_SMART = os.environ.get("MODEL_SMART", "openai/gpt-oss-120b")
MODEL_GEMINI = os.environ.get("MODEL_GEMINI", "gemini-3.5-flash-lite")
MODEL_WHISPER = os.environ.get("MODEL_WHISPER", "whisper-large-v3-turbo")

# Voice language hint. Default Persian; set WHISPER_LANGUAGE to an empty string for auto-detect.
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "fa").strip() or None

# ---------------------------------------------------------------- limits
MAX_AUDIO_SECONDS = 300          # protects the free Whisper quota
LONG_MODE_MAX_TOKENS = 4096      # Groq output cap in "long" mode
MAX_HISTORY_MESSAGES = 10        # conversation memory window (messages, not turns)

# Telegram/Bale hard limit is 4096 chars. Telegram gets a larger safety margin because
# HTML tags are added after splitting and count towards the limit.
TG_CHUNK_SIZE = 3500
BALE_CHUNK_SIZE = 3800

# Link summarisation (Tavily Extract)
LINK_MAX_COUNT = 2           # max links processed per message
LINK_MAX_CHARS = 5000        # total page text handed to the model (keeps the token budget bounded)
LINK_MAX_EXTRA_TEXT = 600    # longer accompanying text => the message is pasted content, not a link request

# Student tools
MAX_PDF_BYTES = 15 * 1024 * 1024  # inline upload to Gemini; base64 adds ~33% on top of this
READ_MAX_CHARS = 12000            # /read output cap
WIKI_MAX_CHARS = 12000            # /wiki output cap
ARXIV_RESULTS = 5                 # /arxiv search results
QUIZ_DEFAULT_TOPIC = "IELTS vocabulary"

# ---------------------------------------------------------------- API key rotation (2+ keys per provider)
# All of these can be overridden through an environment variable of the same name. Seconds unless noted.
KEY_MAX_ATTEMPTS = _env_number("KEY_MAX_ATTEMPTS", 3, int, 1)            # keys tried for one request
KEY_RETRY_BUDGET_SECONDS = _env_number("KEY_RETRY_BUDGET_SECONDS", 90.0, float, 1)  # no new attempt after this
KEY_COOLDOWN_RATE_LIMIT = _env_number("KEY_COOLDOWN_RATE_LIMIT", 60.0)   # 429 without a Retry-After header
KEY_COOLDOWN_RATE_LIMIT_MAX = _env_number("KEY_COOLDOWN_RATE_LIMIT_MAX", 300.0)  # cap for Retry-After
KEY_COOLDOWN_TRANSIENT = _env_number("KEY_COOLDOWN_TRANSIENT", 15.0)     # timeout, connection error, 5xx
KEY_COOLDOWN_AUTH = _env_number("KEY_COOLDOWN_AUTH", 3600.0)             # 401 / 403: the key is probably dead

# ---------------------------------------------------------------- smart web search
# A tiny LLM "planner" call decides whether a message needs fresh web data and writes the search query from the last few
# messages. Any planner problem falls back to the keyword heuristic, so it can never block an answer. The first three can be
# overridden through an environment variable of the same name.
SEARCH_PLANNER_ENABLED = _env_bool("SEARCH_PLANNER_ENABLED", True)        # false = keyword heuristic only
SEARCH_PLANNER_TIMEOUT_SECONDS = _env_number("SEARCH_PLANNER_TIMEOUT_SECONDS", 4.0, float, 0.5)  # per planner call
SEARCH_CACHE_TTL_SECONDS = _env_number("SEARCH_CACHE_TTL_SECONDS", 600.0)  # identical queries; 0 = no cache
SEARCH_CACHE_MAX_ENTRIES = 128         # the cache never holds more queries than this (oldest dropped first)
SEARCH_PLANNER_HISTORY_MESSAGES = 4    # recent messages the planner sees (memory must be on for there to be any)
SEARCH_MAX_SOURCES = 3                 # links appended under an answer that used web results

# ---------------------------------------------------------------- per-user rate limit
# At most RATE_LIMIT_MAX AI-consuming requests per RATE_LIMIT_WINDOW_SECONDS, counted separately for every
# (platform, user id). RATE_LIMIT_MAX=0 switches the limit off. Both can be overridden through the environment.
RATE_LIMIT_MAX = _env_number("RATE_LIMIT_MAX", 20, int, 0)
RATE_LIMIT_WINDOW_SECONDS = _env_number("RATE_LIMIT_WINDOW_SECONDS", 60.0, float, 1)

# ---------------------------------------------------------------- endpoints
BALE_API_BASE = "https://tapi.bale.ai"
