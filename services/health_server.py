"""Tiny HTTP server for platform health checks.

Free hosting tiers (Render, Railway ...) expect a web service to listen on $PORT and
put idle services to sleep; an external uptime monitor can ping ``/`` to keep it awake.
"""
import logging
import threading

from flask import Flask

import config

log = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__)

    @app.route('/')
    def home():
        return "Resilient Multimodal AI Gateway (Telegram + Bale) is online."

    @app.route('/healthz')
    def healthz():
        return {"status": "ok"}

    return app


def start_in_background():
    """Start the server on a daemon thread and return the thread."""
    app = create_app()
    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config.PORT),
        name="health-server",
        daemon=True,
    )
    thread.start()
    return thread
