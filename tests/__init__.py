"""Test package bootstrap.

Runs before any test module is imported, so that ``config`` reads deterministic fake
credentials (never the developer's real ones) and missing SDKs are replaced by stubs.
"""
import logging
import os

logging.getLogger().addHandler(logging.NullHandler())  # keep test output quiet

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TELEGRAM-TOKEN",
    "BALE_BOT_TOKEN": "TEST-BALE-TOKEN",
    "GROQ_API_KEY": "test-groq-key",
    "GEMINI_API_KEY": "test-gemini-key",
    "TAVILY_API_KEY": "test-tavily-key",
    "ALLOWED_USERS": "",
})

from tests import stubs  # noqa: E402

stubs.install_all()
