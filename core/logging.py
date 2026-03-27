"""
SignalEdge — Structured Logging
================================
Configures a JSON logger for production use on Railway.
Each log line is a single JSON object, making it filterable by pair, signal,
level, etc. in Railway's log viewer.

Usage:
    from core.logging import get_logger
    log = get_logger(__name__)
    log.info("signal_evaluated", extra={"pair": "EUR/USD", "signal": "long"})
"""

import logging
import os
import sys

_configured = False


def setup_logging() -> None:
    """Configure root logger once. Safe to call multiple times."""
    global _configured
    if _configured:
        return

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)

    try:
        from pythonjsonlogger import jsonlogger
        fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
        handler.setFormatter(jsonlogger.JsonFormatter(fmt))
    except ImportError:
        # Fallback: plain text if python-json-logger is not installed
        fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
        handler.setFormatter(logging.Formatter(fmt))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, ensuring logging is configured first."""
    setup_logging()
    return logging.getLogger(name)
