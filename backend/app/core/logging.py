"""structlog configuration. JSON output, correlation ids, no secrets logged."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_REDACTED = "***REDACTED***"
_SECRET_KEYS = {
    "password",
    "password_hash",
    "jwt",
    "access_token",
    "refresh_token",
    "agent_key",
    "api_key",
    "hmac_secret",
    "hmac_secret_enc",
    "totp_secret",
    "authorization",
}


def _redact_secrets(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if key.lower() in _SECRET_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def configure_logging(
    *, log_level: str = "INFO", log_format: str = "json", git_sha: str = "unknown"
) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact_secrets,
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(git_sha=git_sha)


def get_logger(**initial_values: Any) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(**initial_values)
    return logger
