"""Structured logging with mandatory redaction.

The redaction processor runs before the renderer, so no sink — stdout, a file, or
an error tracker — can ever see a token, session string, phone number, login
code, or 2FA password.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

from app.security.redaction import redaction_processor

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def _add_correlation_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    value = correlation_id.get()
    if value:
        event_dict.setdefault("correlation_id", value)
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_correlation_id,
            # Always immediately before the renderer.
            redaction_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    for noisy in ("telethon", "aiogram", "asyncio", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
