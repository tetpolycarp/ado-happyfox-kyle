"""
DLQ Monitor — Timer-triggered Azure Function.

Runs every 5 minutes to check for messages in the dead-letter sub-queues
of both ado-parent-events and ado-child-events.

When DLQ messages are found, logs at ERROR level with message details.
Azure Monitor alert rules on the Service Bus "DeadletteredMessages" metric
handle email/Teams notifications — this function provides the detailed
logging in Application Insights for investigation.

Messages are NOT removed from the DLQ — they stay until manually investigated
and purged via the health_check endpoint (?action=dlq_purge) or Azure Portal.
"""

from __future__ import annotations

import logging
from typing import Any

import azure.functions as func

from src.integration.config import settings
from src.integration.services.servicebus_client import peek_dead_letter_messages
from src.integration.utils.logging_config import configure_logging

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

# Track already-logged messages to avoid duplicate log entries.
# Keyed by (queue_name, sequence_number). Persists across invocations
# within the same function host instance (cleared on restart/redeploy).
_reported_messages: set[tuple[str, int | None]] = set()


def _check_queue_dlq(queue_name: str) -> list[dict[str, Any]]:
    """Check a queue's DLQ and return any NEW (not yet reported) messages."""
    try:
        all_messages = peek_dead_letter_messages(queue_name, max_messages=20)
    except Exception as e:
        logger.error(
            "Failed to peek DLQ",
            extra={"queue": queue_name, "error": str(e), "action": "dlq_peek_failed"},
        )
        return []

    new_messages: list[dict[str, Any]] = []
    for msg in all_messages:
        seq = msg.get("sequence_number")
        key = (queue_name, seq)
        if key not in _reported_messages:
            new_messages.append(msg)
            _reported_messages.add(key)

    if all_messages:
        logger.error(
            "DLQ messages found",
            extra={
                "queue": queue_name,
                "total_dlq_count": len(all_messages),
                "new_count": len(new_messages),
                "action": "dlq_messages_found",
            },
        )

    return new_messages


def main(timer: func.TimerRequest) -> None:
    """
    Timer trigger entry point — checks both queues' DLQs every 5 minutes.

    Logs ERROR with full message details for any newly discovered DLQ messages.
    Azure Monitor metric alerts on DeadletteredMessages handle email notifications.
    """
    new_messages: list[dict[str, Any]] = []
    new_messages.extend(_check_queue_dlq(settings.ado_parent_events_queue))
    new_messages.extend(_check_queue_dlq(settings.ado_child_events_queue))

    if not new_messages:
        logger.debug("DLQ monitor — no new dead-letter messages")
        return

    # Log each new DLQ message with full details for App Insights investigation
    for msg in new_messages:
        logger.error(
            "Dead-lettered message requires investigation",
            extra={
                "queue": msg.get("queue"),
                "message_id": msg.get("message_id"),
                "dead_letter_reason": msg.get("dead_letter_reason"),
                "dead_letter_description": msg.get("dead_letter_description"),
                "delivery_count": msg.get("delivery_count"),
                "enqueued_time": msg.get("enqueued_time"),
                "sequence_number": msg.get("sequence_number"),
                "body_preview": msg.get("body", "")[:300],
                "action": "dlq_message_detail",
            },
        )

    logger.error(
        "DLQ alert — %d new dead-lettered message(s) found",
        len(new_messages),
        extra={
            "new_message_count": len(new_messages),
            "queues_affected": list({m.get("queue") for m in new_messages}),
            "action": "dlq_alert_summary",
        },
    )
