"""Central configuration: environment variables, model names and tunable limits.

Every other module reads values through ``config.NAME`` at call time (never
``from config import NAME``), so tests can override a value with
``mock.patch.object(config, "NAME", value)``.
"""
import os


def _parse_user_ids(raw):
    return [int(uid.strip()) for uid in (raw or "").split(",") if uid.strip().isdigit()]


# ---------------------------------------------------------------- secrets / credentials
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")

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

# ---------------------------------------------------------------- endpoints
BALE_API_BASE = "https://tapi.bale.ai"
