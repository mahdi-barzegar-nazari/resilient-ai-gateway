"""Logging setup with automatic secret redaction."""
import logging

from utils.security import redact_secrets

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


class RedactingFormatter(logging.Formatter):
    """Scrubs secrets from the *final* log line, including exception tracebacks."""

    def format(self, record):
        return redact_secrets(super().format(record))


def setup_logging(level=logging.INFO):
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    # urllib3 logs full request URLs at DEBUG; those URLs contain bot tokens
    logging.getLogger("urllib3").setLevel(logging.WARNING)
