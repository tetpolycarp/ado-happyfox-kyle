"""Service Bus queue diagnostic actions: queue_status, peek_dlq, replay_dlq."""
from __future__ import annotations

import traceback
from typing import Any

import azure.functions as func

from health_check._helpers import run_diagnostic


# ---------------------------------------------------------------------------
# queue_status
# ---------------------------------------------------------------------------
@run_diagnostic
def queue_status(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Check message counts in parent and child Service Bus queues."""
    from src.integration.config import settings
    from azure.servicebus.management import ServiceBusAdministrationClient

    conn_str = settings.service_bus_connection_string.get_secret_value()
    admin_client = ServiceBusAdministrationClient.from_connection_string(conn_str)

    for queue_name in [settings.ado_parent_events_queue, settings.ado_child_events_queue]:
        try:
            runtime_props = admin_client.get_queue_runtime_properties(queue_name)
            results[queue_name] = {
                "active_message_count": runtime_props.active_message_count,
                "dead_letter_message_count": runtime_props.dead_letter_message_count,
                "scheduled_message_count": runtime_props.scheduled_message_count,
                "transfer_message_count": runtime_props.transfer_message_count,
                "total_message_count": runtime_props.total_message_count,
            }
            results["steps"].append(
                f"{queue_name}: active={runtime_props.active_message_count}, "
                f"dlq={runtime_props.dead_letter_message_count}"
            )
        except Exception as e:
            results[f"{queue_name}_error"] = str(e)
            results["steps"].append(f"{queue_name}: ERROR — {e}")

    admin_client.close()

    # Smoke test: verify service imports work
    results["steps"].append("Testing child processor imports...")
    try:
        from src.integration.services.ado_service import AdoService
        from src.integration.services.happyfox_service import HappyFoxService
        ado = AdoService(settings)
        hf = HappyFoxService(settings)
        ado.close()
        hf.close()
        results["import_test"] = "OK"
        results["steps"].append("Imports OK — AdoService and HappyFoxService instantiate without error")
    except Exception as e:
        results["import_test"] = f"FAILED: {e}"
        results["import_traceback"] = traceback.format_exc()
        results["steps"].append(f"Import test FAILED: {e}")


# ---------------------------------------------------------------------------
# peek_dlq
# ---------------------------------------------------------------------------
@run_diagnostic
def peek_dlq(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Peek at messages in the dead letter queue to see failure reasons."""
    queue_name = req.params.get("queue", "ado-child-events")
    count = int(req.params.get("count", "3"))
    results["queue"] = queue_name
    results["requested_count"] = count

    from src.integration.config import settings
    from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode

    conn_str = settings.service_bus_connection_string.get_secret_value()
    sb_client = ServiceBusClient.from_connection_string(conn_str)

    dlq_path = f"{queue_name}/$deadletterqueue"
    results["steps"].append(f"Peeking at {dlq_path}...")

    with sb_client.get_queue_receiver(
        queue_name,
        sub_queue="deadletter",
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        max_wait_time=10,
    ) as receiver:
        messages = receiver.peek_messages(max_message_count=count)
        results["message_count"] = len(messages)
        results["steps"].append(f"Found {len(messages)} DLQ messages")

        dlq_messages = []
        for msg in messages:
            entry = {
                "dead_letter_reason": msg.dead_letter_reason,
                "dead_letter_description": msg.dead_letter_error_description,
                "delivery_count": msg.delivery_count,
                "enqueued_time": str(msg.enqueued_time_utc),
                "sequence_number": msg.sequence_number,
                "body_preview": str(msg)[:500],
            }
            dlq_messages.append(entry)
            results["steps"].append(
                f"  Message #{msg.sequence_number}: "
                f"reason='{msg.dead_letter_reason}', "
                f"desc='{msg.dead_letter_error_description}', "
                f"deliveries={msg.delivery_count}"
            )

        results["messages"] = dlq_messages

    sb_client.close()


# ---------------------------------------------------------------------------
# replay_dlq
# ---------------------------------------------------------------------------
@run_diagnostic
def replay_dlq(req: func.HttpRequest, results: dict[str, Any]) -> None:
    """Replay messages from the dead letter queue back to the active queue."""
    queue_name = req.params.get("queue", "ado-child-events")
    count = int(req.params.get("count", "5"))
    dry_run = req.params.get("dry_run", "true").lower() != "false"
    results["queue"] = queue_name
    results["dry_run"] = dry_run
    results["requested_count"] = count

    from src.integration.config import settings
    from src.integration.services.ado_service import AdoService
    from src.integration.models.events import IntegrationEvent
    from azure.servicebus import ServiceBusClient, ServiceBusReceiveMode

    conn_str = settings.service_bus_connection_string.get_secret_value()
    sb_client = ServiceBusClient.from_connection_string(conn_str)
    ado = AdoService(settings)

    replayed = 0
    skipped = 0
    failed = 0

    try:
        with sb_client.get_queue_receiver(
            queue_name,
            sub_queue="deadletter",
            receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
            max_wait_time=10,
        ) as receiver:
            messages = receiver.receive_messages(max_message_count=count, max_wait_time=10)
            results["dlq_message_count"] = len(messages)
            results["steps"].append(f"Received {len(messages)} DLQ messages")

            for msg in messages:
                body = str(msg)
                try:
                    event = IntegrationEvent.model_validate_json(body)
                    work_item_id = int(event.resource_id)

                    try:
                        ado.get_work_item(work_item_id)
                        exists = True
                    except Exception:
                        exists = False

                    if not exists:
                        results["steps"].append(
                            f"  SKIP {event.resource_id} ({event.client}) — work item no longer exists"
                        )
                        if not dry_run:
                            receiver.complete_message(msg)
                        skipped += 1
                        continue

                    if dry_run:
                        results["steps"].append(
                            f"  WOULD REPLAY {event.resource_id} ({event.client}) — work item exists"
                        )
                        replayed += 1
                    else:
                        from src.integration.services.servicebus_client import send_to_child_queue, send_to_parent_queue
                        if "child" in queue_name:
                            send_to_child_queue(body)
                        else:
                            send_to_parent_queue(body)
                        receiver.complete_message(msg)
                        results["steps"].append(f"  REPLAYED {event.resource_id} ({event.client})")
                        replayed += 1

                except Exception as e:
                    results["steps"].append(f"  ERROR processing DLQ message: {e}")
                    failed += 1

        results["replayed"] = replayed
        results["skipped"] = skipped
        results["failed"] = failed

    finally:
        ado.close()
        sb_client.close()
