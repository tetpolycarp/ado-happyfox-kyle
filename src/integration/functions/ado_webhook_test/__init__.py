"""
ADO Webhook Test — Bare-bones HTTP trigger for payload inspection.

This is a temporary function for testing. It accepts ANY POST request,
logs the full raw body and headers, and returns the parsed JSON back
to the caller. No HMAC validation, no trigger detection — just capture
and echo the payload so we can see the exact ADO field reference names.

IMPORTANT: Remove or disable this function before production deployment.
It has no authentication and exposes payload data in responses.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import azure.functions as func

logger = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger that captures and logs the full ADO webhook payload.

    Returns the parsed payload as the response body for easy inspection.
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    # Capture raw body
    raw_body = req.get_body()
    body_str = raw_body.decode("utf-8", errors="replace")

    # Capture headers
    headers = dict(req.headers)

    # Log everything
    logger.info("=" * 80)
    logger.info(f"ADO WEBHOOK TEST — Received at {timestamp}")
    logger.info("=" * 80)
    logger.info(f"METHOD: {req.method}")
    logger.info(f"URL: {req.url}")
    logger.info(f"HEADERS: {json.dumps(headers, indent=2)}")
    logger.info(f"BODY LENGTH: {len(raw_body)} bytes")
    logger.info(f"RAW BODY:\n{body_str}")

    # Try to parse as JSON
    try:
        parsed = json.loads(body_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse body as JSON: {e}")
        return func.HttpResponse(
            json.dumps({
                "status": "received_but_not_json",
                "timestamp": timestamp,
                "body_length": len(raw_body),
                "raw_body_preview": body_str[:2000],
                "error": str(e),
            }, indent=2),
            mimetype="application/json",
            status_code=200,
        )

    # Extract key fields for quick reference
    summary = _extract_summary(parsed)

    # Log the summary
    logger.info(f"SUMMARY: {json.dumps(summary, indent=2)}")

    # Build response with full payload + summary
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


def _extract_summary(payload: dict) -> dict:
    """
    Extract a quick-reference summary from an ADO webhook payload.

    Pulls out the most useful bits: event type, work item type, changed fields,
    and all field reference names with their values.
    """
    summary: dict = {
        "event_type": payload.get("eventType", "unknown"),
        "subscription_id": payload.get("subscriptionId", "unknown"),
    }

    resource = payload.get("resource", {})
    summary["work_item_id"] = resource.get("workItemId") or resource.get("id")
    summary["revision_number"] = resource.get("rev")

    # Changed fields (the "fields" dict at resource level for workitem.updated)
    changed_fields = resource.get("fields", {})
    if changed_fields:
        summary["changed_fields"] = {}
        for field_ref, change in changed_fields.items():
            if isinstance(change, dict):
                summary["changed_fields"][field_ref] = {
                    "old": change.get("oldValue"),
                    "new": change.get("newValue"),
                }
            else:
                summary["changed_fields"][field_ref] = change

    # Full current fields from the revision
    revision = resource.get("revision", {})
    revision_fields = revision.get("fields", {})
    if revision_fields:
        summary["all_field_reference_names"] = sorted(revision_fields.keys())
        summary["all_fields_with_values"] = {
            k: v for k, v in sorted(revision_fields.items())
        }

    # Revised by
    revised_by = resource.get("revisedBy", {})
    if revised_by:
        summary["revised_by"] = revised_by.get("displayName", "unknown")

    return summary
