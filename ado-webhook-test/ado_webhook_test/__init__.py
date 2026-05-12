"""
ADO Webhook Test — Bare-bones payload inspector.

Accepts any POST, logs the full raw body and headers, extracts all ADO field
reference names, and returns everything as JSON. No auth, no dependencies
beyond azure-functions.

TEMPORARY — remove after capturing field reference names.
"""

import json
import logging
from datetime import datetime, timezone

import azure.functions as func

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    timestamp = datetime.now(timezone.utc).isoformat()
    raw_body = req.get_body()
    body_str = raw_body.decode("utf-8", errors="replace")

    logger.info("=" * 80)
    logger.info(f"ADO WEBHOOK TEST received at {timestamp}")
    logger.info(f"Headers: {json.dumps(dict(req.headers), indent=2)}")
    logger.info(f"Body ({len(raw_body)} bytes):\n{body_str}")
    logger.info("=" * 80)

    # Try to parse as JSON
    try:
        parsed = json.loads(body_str)
    except json.JSONDecodeError as e:
        return func.HttpResponse(
            json.dumps({
                "status": "received_but_not_json",
                "timestamp": timestamp,
                "body_length": len(raw_body),
                "raw_body_preview": body_str[:2000],
                "parse_error": str(e),
            }, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    # Extract summary from ADO webhook payload
    resource = parsed.get("resource", {})
    revision = resource.get("revision", {})
    revision_fields = revision.get("fields", {})
    changed_fields = resource.get("fields", {})

    summary = {
        "event_type": parsed.get("eventType"),
        "work_item_id": resource.get("workItemId") or resource.get("id"),
        "revision_number": resource.get("rev"),
        "work_item_type": revision_fields.get("System.WorkItemType", "unknown"),
        "title": revision_fields.get("System.Title", "unknown"),
        "state": revision_fields.get("System.State", "unknown"),
        "changed_fields": {
            k: {"old": v.get("oldValue"), "new": v.get("newValue")}
            for k, v in changed_fields.items()
            if isinstance(v, dict)
        },
        "all_field_reference_names": sorted(revision_fields.keys()),
        "all_fields_with_values": dict(sorted(
            (k, _safe_value(v)) for k, v in revision_fields.items()
        )),
    }

    logger.info(f"SUMMARY:\n{json.dumps(summary, indent=2, default=str)}")

    response = {
        "status": "received",
        "timestamp": timestamp,
        "summary": summary,
        "full_payload": parsed,
    }

    return func.HttpResponse(
        json.dumps(response, indent=2, default=str),
        mimetype="application/json",
        status_code=200,
    )


def _safe_value(v):
    """Truncate long values for readability."""
    if isinstance(v, str) and len(v) > 500:
        return v[:500] + "... (truncated)"
    return v
