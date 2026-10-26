"""Entry point: wires the platform adapters together and starts them.

Thread layout
-------------
* health server  - daemon thread (HTTP 200 on / for the hosting platform)
* Bale polling   - daemon thread (only when BALE_BOT_TOKEN is set)
* Telegram       - main thread   (long polling; blocks)
If Telegram is not configured, Bale runs on the main thread instead.
"""
import logging
import sys
import threading

import config
from utils.log import setup_logging

log = logging.getLogger("gateway")


def main():
    setup_logging()

    if not config.TELEGRAM_BOT_TOKEN and not config.BALE_BOT_TOKEN:
        log.error("Neither TELEGRAM_BOT_TOKEN nor BALE_BOT_TOKEN is set; nothing to run. See .env.example.")
        return 1
    if not config.api_keys("groq") and not config.api_keys("gemini"):
        log.warning("No GROQ_API_KEY(S) / GEMINI_API_KEY(S) configured: the bot will reply that AI services are unavailable.")
    log.info("API keys loaded: groq=%d gemini=%d tavily=%d",
             len(config.api_keys("groq")), len(config.api_keys("gemini")), len(config.api_keys("tavily")))

    # Imported here so that configuration errors above are reported before heavy SDK imports.
    from services.health_server import start_in_background
    from bot.bale_handler import BaleApp
    from bot.telegram_handler import TelegramApp

    start_in_background()

    bale = BaleApp(config.BALE_BOT_TOKEN) if config.BALE_BOT_TOKEN else None
    telegram = TelegramApp(config.TELEGRAM_BOT_TOKEN) if config.TELEGRAM_BOT_TOKEN else None

    if bale and telegram:
        threading.Thread(target=bale.run, name="bale-polling", daemon=True).start()
        log.info("Gateway started: Telegram (main thread) + Bale (background thread).")
        telegram.run()
    elif telegram:
        log.info("Gateway started: Telegram only (BALE_BOT_TOKEN not set).")
        telegram.run()
    else:
        log.info("Gateway started: Bale only (TELEGRAM_BOT_TOKEN not set).")
        bale.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
