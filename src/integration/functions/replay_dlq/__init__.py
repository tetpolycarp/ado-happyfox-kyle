"""
Replay DLQ — HTTP-triggered Azure Function.

Exposes the DLQ replay helper as an HTTP endpoint so ops can trigger a
bulk retry from the browser (bookmark), curl, or Postman after an outage
without SSHing or running the CLI script locally.

Authentication: Azure Function key (`?code=<function_key>`). Add the key
from Portal → Function App → Functions → replay_dlq → Function Keys.

Query parameters
----------------
  queue     Queue name to drain (ado-parent-events | ado-child-events).
            Omit when using all=true.
  all       "true" to replay BOTH integration queues back-to-back.
  max       Max messages to replay per queue (default 500, hard cap 5000).
  dry_run   "true" to preview only — logs what would be replayed but does
            not re-enqueue or remove anything.

Examples
--------
  # Preview child-queue DLQ
  GET /api/replay-dlq?queue=ado-child-events&dry_run=true&code=KEY

  # Replay up to 1000 child-queue DLQ messages
  GET /api/replay-dlq?queue=ado-child-events&max=1000&code=KEY

  # Drain both queues
  GET /api/replay-dlq?all=true&max=2000&code=KEY

Returns JSON: {"ok": bool, "results": [{"queue", "replayed", "failed", "remaining_seen"}]}
"""

from __future__ import annotations

import json
import logging

import azure.functions as func

from src.integration.config import settings
from src.integration.services.servicebus_client import replay_dead_letter_messages
from src.integration.utils.logging_config import configure_logging

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

# Hard safety cap — protects against a runaway max= value
MAX_MESSAGES_HARD_CAP = 5000

# Only known integration queues are replayable — guards against typos
# and prevents the endpoint being pointed at unrelated queues.
_ALLOWED_QUEUES = {
    settings.ado_parent_events_queue,
    settings.ado_child_events_queue,
}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y"}


def _resolve_queues(req: func.HttpRequest) -> tuple[list[str] | None, str | None]:
    """Return (queues, error_message). Exactly one of queues/error is set."""
    if _truthy(req.params.get("all")):
        return [
            settings.ado_parent_events_queue,
            settings.ado_child_events_queue,
        ], None

    queue = (req.params.get("queue") or "").strip()
    if not queue:
        return None, "Missing required parameter: 'queue' (or pass all=true)."
    if queue not in _ALLOWED_QUEUES:
        return None, (
            f"Queue '{queue}' is not in the allowed list. "
            f"Allowed: {sorted(_ALLOWED_QUEUES)}"
        )
    return [queue], None


def _resolve_max(req: func.HttpRequest) -> tuple[int | None, str | None]:
    raw = req.params.get("max")
    if raw is None or raw == "":
        return 500, None
    try:
        value = int(raw)
    except ValueError:
        return None, f"Invalid 'max' value: {raw!r} (must be integer)."
    if value <= 0:
        return None, "'max' must be positive."
    if value > MAX_MESSAGES_HARD_CAP:
        return None, f"'max' exceeds hard cap of {MAX_MESSAGES_HARD_CAP}."
    return value, None


def main(req: func.HttpRequest) -> func.HttpResponse:
    dry_run = _truthy(req.params.get("dry_run"))

    queues, err = _resolve_queues(req)
    if err:
        return func.HttpResponse(
            json.dumps({"ok": False, "error": err}),
            status_code=400,
            mimetype="application/json",
        )

    max_messages, err = _resolve_max(req)
    if err:
        return func.HttpResponse(
            json.dumps({"ok": False, "error": err}),
            status_code=400,
            mimetype="application/json",
        )

    logger.info(
        "DLQ replay HTTP trigger invoked",
        extra={
            "queues": queues,
            "max_messages": max_messages,
            "dry_run": dry_run,
            "action": "dlq_replay_http",
        },
    )

    results: list[dict] = []
    any_failed = False
    for queue in queues:
        try:
            result = replay_dead_letter_messages(
                queue_name=queue,
                max_messages=max_messages,
                dry_run=dry_run,
            )
            results.append({"queue": queue, **result})
            if result.get("failed", 0) > 0:
                any_failed = True
        except Exception as e:
            logger.error(
                "DLQ replay failed for queue",
                extra={"queue": queue, "error": str(e), "action": "dlq_replay_http"},
            )
            results.append({
                "queue": queue,
                "error": str(e),
                "replayed": 0,
                "failed": 0,
                "remaining_seen": 0,
            })
            any_failed = True

    status = 200 if not any_failed else 207  # 207 Multi-Status on partial failure
    return func.HttpResponse(
        json.dumps({
            "ok": not any_failed,
            "dry_run": dry_run,
            "max_per_queue": max_messages,
            "results": results,
        }, indent=2),
        status_code=status,
        mimetype="application/json",
    )
