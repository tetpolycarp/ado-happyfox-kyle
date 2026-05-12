"""
Structured logging setup for Azure Functions with Application Insights.

Produces JSON-formatted log entries with correlation IDs as custom dimensions.
Application Insights automatically captures structured log extras.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """
    Formats log records as JSON for structured ingestion by Application Insights.

    Extra fields (ado_id, hf_ticket_id, action, etc.) are included as top-level keys
    so they appear as custom dimensions in Application Insights queries.
    """

    # Fields from the standard LogRecord that we include in the JSON output
    STANDARD_FIELDS = {"message", "levelname", "name", "funcName", "lineno"}

    # Fields from the standard LogRecord that we exclude (noise)
    EXCLUDED_FIELDS = {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "levelno",
        "module",
        "msecs",
        "msg",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Include exception info if present
        if record.exc_info and record.exc_info[1] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Include any extra fields as custom dimensions
        for key, value in record.__dict__.items():
            if key not in self.STANDARD_FIELDS and key not in self.EXCLUDED_FIELDS:
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    """
    Configure the root logger with JSON structured output.

    Call this once at module load time in each Azure Function entry point.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    root_logger = logging.getLogger()

    # Avoid duplicate handlers if called multiple times
    if any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter) for h in root_logger.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Reduce noise from Azure SDK and httpx loggers
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
