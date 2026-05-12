"""
Service Bus client — sends and receives messages using the azure-servicebus SDK.

Used instead of Azure Functions bindings because Flex Consumption plans
silently fail to load functions that declare Service Bus bindings.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode

from src.integration.config import settings

logger = logging.getLogger(__name__)

# Module-level client — reused across invocations for connection pooling
_client: ServiceBusClient | None = None


def _get_client() -> ServiceBusClient:
    """Lazy-init a shared ServiceBusClient."""
    global _client
    if _client is None:
        conn_str = settings.service_bus_connection_string.get_secret_value()
        _client = ServiceBusClient.from_connection_string(conn_str)
    return _client


def send_to_queue(queue_name: str, body: dict[str, Any] | str) -> None:
    """Send a single message to a Service Bus queue."""
    if isinstance(body, dict):
        body = json.dumps(body)

    client = _get_client()
    with client.get_queue_sender(queue_name) as sender:
        sender.send_messages(ServiceBusMessage(body))

    logger.info("Sent message to queue %s", queue_name)


def send_to_parent_queue(body: dict[str, Any] | str) -> None:
    """Send a message to the parent events queue."""
    send_to_queue(settings.ado_parent_events_queue, body)


def send_to_child_queue(body: dict[str, Any] | str) -> None:
    """Send a message to the child events queue."""
    send_to_queue(settings.ado_child_events_queue, body)


def receive_and_process_from_queue(
    queue_name: str,
    processor: Callable[[str], None],
    max_messages: int = 10,
    max_wait_secs: int = 5,
) -> tuple[int, int]:
    """
    Receive messages and process each one before completing it.

    Completes a message only AFTER the processor callback succeeds.
    If processing fails, the message is abandoned
    and will be retried by Service Bus (up to the queue's max delivery count).

    Args:
        queue_name: Queue to receive from.
        processor: Callback that processes a single message body string.
                   Should raise an exception if processing fails.
        max_messages: Max messages to receive in one batch.
        max_wait_secs: Max seconds to wait for messages.

    Returns:
        Tuple of (processed_count, failed_count).
    """
    client = _get_client()
    processed = 0
    failed = 0

    with client.get_queue_receiver(
        queue_name,
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        max_wait_time=max_wait_secs,
    ) as receiver:
        messages = receiver.receive_messages(
            max_message_count=max_messages,
            max_wait_time=max_wait_secs,
        )

        if messages:
            logger.info("Received %d messages from queue %s", len(messages), queue_name)

        for msg in messages:
            body = str(msg)
            try:
                processor(body)
                receiver.complete_message(msg)
                processed += 1
            except Exception as e:
                logger.error(
                    "Processing failed, abandoning message for retry",
                    extra={
                        "queue": queue_name,
                        "error": str(e),
                        "raw_body": body[:200],
                    },
                )
                try:
                    receiver.abandon_message(msg)
                except Exception as abandon_err:
                    logger.error("Failed to abandon message: %s", abandon_err)
                failed += 1

    return processed, failed


def peek_dead_letter_messages(
    queue_name: str,
    max_messages: int = 10,
) -> list[dict[str, Any]]:
    """
    Peek at messages in a queue's dead-letter sub-queue WITHOUT removing them.

    Returns a list of dicts with message metadata and body for notification
    purposes. Messages stay in the DLQ until explicitly completed or purged.
    """
    from azure.servicebus import ServiceBusReceiveMode

    client = _get_client()
    dlq_messages: list[dict[str, Any]] = []

    with client.get_queue_receiver(
        queue_name,
        sub_queue="deadletter",
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        max_wait_time=5,
    ) as receiver:
        messages = receiver.peek_messages(max_message_count=max_messages)

        for msg in messages:
            dlq_messages.append({
                "queue": queue_name,
                "message_id": msg.message_id,
                "body": str(msg)[:500],
                "dead_letter_reason": msg.dead_letter_reason,
                "dead_letter_description": msg.dead_letter_error_description,
                "enqueued_time": str(msg.enqueued_time_utc) if msg.enqueued_time_utc else "",
                "delivery_count": msg.delivery_count,
                "sequence_number": msg.sequence_number,
            })

    return dlq_messages


def complete_dead_letter_messages(
    queue_name: str,
    max_messages: int = 10,
) -> int:
    """
    Receive and complete (remove) messages from a queue's dead-letter sub-queue.

    Use after DLQ messages have been investigated and resolved.
    Returns the number of messages removed.
    """
    client = _get_client()
    removed = 0

    with client.get_queue_receiver(
        queue_name,
        sub_queue="deadletter",
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        max_wait_time=5,
    ) as receiver:
        messages = receiver.receive_messages(
            max_message_count=max_messages,
            max_wait_time=5,
        )
        for msg in messages:
            try:
                receiver.complete_message(msg)
                removed += 1
            except Exception as e:
                logger.error("Failed to complete DLQ message: %s", e)

    return removed


def replay_dead_letter_messages(
    queue_name: str,
    max_messages: int = 100,
    batch_size: int = 10,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Replay (resubmit) messages from a queue's DLQ back onto the main queue.

    For each DLQ message, sends a fresh copy to the main queue first. Only
    completes (removes) the DLQ message AFTER the re-enqueue succeeds — so
    nothing is lost if re-enqueue fails mid-way. Runs in batches to avoid
    lock-renewal issues on large DLQs.

    Args:
        queue_name: The main queue whose DLQ will be drained.
        max_messages: Safety cap on total messages processed this run.
        batch_size: Messages fetched per receiver session (lock window).
        dry_run: If True, peek-only — logs what would be replayed but
                 does not re-enqueue or remove anything.

    Returns:
        {"replayed": int, "failed": int, "remaining_seen": int}
    """
    client = _get_client()
    replayed = 0
    failed = 0
    total_seen = 0

    mode = "DRY RUN" if dry_run else "REPLAY"
    logger.info(
        "%s starting for DLQ of %s (max=%d, batch=%d)",
        mode, queue_name, max_messages, batch_size,
    )

    while total_seen < max_messages:
        remaining = max_messages - total_seen
        this_batch = min(batch_size, remaining)

        with client.get_queue_receiver(
            queue_name,
            sub_queue="deadletter",
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            max_wait_time=5,
        ) as receiver:
            messages = receiver.receive_messages(
                max_message_count=this_batch,
                max_wait_time=5,
            )
            if not messages:
                break

            # Use a dedicated sender so re-enqueues happen on the MAIN queue,
            # not the DLQ sub-queue.
            with client.get_queue_sender(queue_name) as sender:
                for msg in messages:
                    total_seen += 1
                    body = str(msg)
                    dl_reason = msg.dead_letter_reason or ""
                    dl_desc = msg.dead_letter_error_description or ""

                    logger.info(
                        "DLQ message under consideration",
                        extra={
                            "queue": queue_name,
                            "message_id": msg.message_id,
                            "sequence_number": msg.sequence_number,
                            "dead_letter_reason": dl_reason,
                            "dead_letter_description": dl_desc[:200],
                            "body_preview": body[:200],
                            "dry_run": dry_run,
                            "action": "dlq_replay",
                        },
                    )

                    if dry_run:
                        # Let the lock expire so the message stays in the DLQ
                        continue

                    try:
                        sender.send_messages(ServiceBusMessage(body))
                    except Exception as e:
                        failed += 1
                        logger.error(
                            "Failed to re-enqueue DLQ message — leaving in DLQ",
                            extra={
                                "queue": queue_name,
                                "message_id": msg.message_id,
                                "error": str(e),
                                "action": "dlq_replay",
                            },
                        )
                        continue

                    try:
                        receiver.complete_message(msg)
                        replayed += 1
                    except Exception as e:
                        # Re-enqueue already succeeded. Message will be
                        # redelivered from DLQ on next run and replayed again —
                        # downstream processors should be idempotent.
                        failed += 1
                        logger.error(
                            "Re-enqueued but failed to complete DLQ copy — "
                            "may result in duplicate replay on retry",
                            extra={
                                "queue": queue_name,
                                "message_id": msg.message_id,
                                "error": str(e),
                                "action": "dlq_replay",
                            },
                        )

    logger.info(
        "%s finished for DLQ of %s — replayed=%d failed=%d seen=%d",
        mode, queue_name, replayed, failed, total_seen,
    )
    return {"replayed": replayed, "failed": failed, "remaining_seen": total_seen}
