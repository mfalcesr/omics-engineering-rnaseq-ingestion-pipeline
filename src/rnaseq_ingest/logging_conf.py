"""Structured (JSON) logging via structlog.

Every log line is machine-parseable and, once bound, carries ``payload_id`` and
``vendor_sample_id`` so a single payload's journey is greppable end to end. This is the
observability contract handed off to an orchestrator (Dagster) unchanged.
"""

from __future__ import annotations

import json
import logging
import sys

import structlog


def _json_serializer(obj, **kwargs) -> str:
    """json.dumps with default=str so Decimals/datetimes never crash a log call."""
    return json.dumps(obj, default=str, **kwargs)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure structlog once at process start.

    :param json_output: emit JSON (production/orchestrator) vs. a colourised console
        renderer (interactive local runs).
    """
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer(serializer=_json_serializer)
        if json_output
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(**initial_context):
    """Return a bound logger seeded with initial context (e.g. payload_id)."""
    return structlog.get_logger().bind(**initial_context)
