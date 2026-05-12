"""
Child Story Processor — Timer-triggered Azure Function.

Polls the ado-child-events queue via SDK. When a child work item (Client Story,
Client Epic, Client Bug, Client Initiative, Client Feature) is created or
updated, this function syncs it to HappyFox:

1. Fetches the Client Story from ADO
2. Checks if a HappyFox ticket already exists:
   a. First checks the ADO SupportTicketNumber field (written back on create)
   b. Falls back to searching HappyFox by "ADO Child ID" custom field (t-cf-45)
3. If exists -> updates the HF ticket
4. If not -> creates a new HF ticket, writes HF ID back to ADO

Supports a "Child Sync Now" event (CLIENT_STORY_SYNC_NOW) for individual child
work items that have Sync to Client Portal set to "Sync Now". After sync
completes, resets the field back to idle state.

NOTE: Uses timer trigger + SDK polling instead of Service Bus trigger bindings
because Flex Consumption plans fail to load functions with Service Bus bindings.

Messages are only completed AFTER successful processing. If processing fails,
the message is abandoned and retried by Service Bus (up to max delivery count).
"""

from __future__ import annotations

import logging

import azure.functions as func

from datetime import datetime, timezone

from src.integration.config import settings
from src.integration.models.ado_models import AdoFieldNames, AdoWorkItemTypes
from src.integration.models.events import EventType, IntegrationEvent
from src.integration.services.ado_service import AdoService
from src.integration.services.happyfox_service import HappyFoxService
from src.integration.services.servicebus_client import receive_and_process_from_queue
from src.integration.services.content_parser import (
    extract_ado_image_urls,
    rewrite_html_images,
)
from src.integration.services.transform_service import (
    ado_client_story_to_happyfox_create,
    ado_client_story_to_happyfox_update,
)
from src.integration.utils.logging_config import configure_logging

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

# Defaults are now loaded from centralized settings (config.py).
# Editable in Azure App Settings without redeploying code.

# ---------------------------------------------------------------------------
# Deduplication window — prevents double-processing the same child when
# multiple parent events (e.g., State change + Sync Now) arrive from the
# same user action. Keyed by child ID → epoch timestamp of last successful
# processing. Persists across invocations within the same function host
# instance (cleared on restart/redeploy).
# ---------------------------------------------------------------------------
import time as _time

_last_processed: dict[int, float] = {}


def _is_recently_processed(child_id: int) -> bool:
    """Return True if this child was successfully processed within the dedup window."""
    last = _last_processed.get(child_id)
    if last is None:
        return False
    return (_time.time() - last) < settings.child_dedup_window_seconds


def _mark_processed(child_id: int) -> None:
    """Record that this child was just processed successfully."""
    _last_processed[child_id] = _time.time()
    # Prune stale entries to avoid unbounded growth
    cutoff = _time.time() - settings.child_dedup_window_seconds * 2
    stale = [k for k, v in _last_processed.items() if v < cutoff]
    for k in stale:
        del _last_processed[k]


def _find_existing_hf_ticket(
    fields: dict,
    hf: HappyFoxService,
    ado_child_id: str,
) -> int | None:
    """
    Check if a HappyFox ticket already exists for this Client Story.

    Lookup order:
    1. Check the ADO "Support Ticket Number" field -> verify ticket exists in HF
    2. Search HappyFox by ADO Child ID custom field (t-cf-45)
    3. Return None if no existing ticket found
    """
    support_ticket_number = fields.get(AdoFieldNames.SUPPORT_TICKET_NUMBER, "")
    if support_ticket_number:
        try:
            hf_id = int(support_ticket_number)
            ticket = hf.get_ticket(hf_id)
            if ticket is not None:
                return ticket.id
            logger.warning(
                "Support Ticket Number in ADO doesn't match any HF ticket",
                extra={
                    "ado_child_id": ado_child_id,
                    "support_ticket_number": support_ticket_number,
                },
            )
        except (ValueError, TypeError):
            logger.warning(
                "Invalid Support Ticket Number format",
                extra={
                    "ado_child_id": ado_child_id,
                    "support_ticket_number": support_ticket_number,
                },
            )

    ticket = hf.find_ticket_by_ado_child_id(ado_child_id)
    if ticket is not None:
        return ticket.id

    return None




def _description_differs(ado_description: str, hf_description: str) -> bool:
    """
    Compare the composed ADO description against the latest HF description.

    Delegates to the shared html_utils module for consistent normalization
    across all comparison paths (ado_service field diff and HF description diff).
    """
    from src.integration.utils.html_utils import html_content_differs

    return html_content_differs(ado_description, hf_description)


def _mirror_parent_for_orphan_child(
    ado: AdoService, child_id: int, project: str = ""
) -> None:
    """
    Create a mirrored parent work item for a HappyFox-originated child.

    Triggered when HappyFox's automation rule creates a Client work item in ADO
    with a Support Ticket Number but no parent link. We create a parent of the
    mirrored (non-client) type, copy the child's content over, and link them.

    Idempotent: if the child already has a parent link, this is a no-op.

    Field copy strategy: everything we can reasonably carry up to the parent —
    title, content fields (Description / AC / Test Scenarios / UI-UX AC),
    priority, request category, area/iteration paths. Client-specific fields
    (Support Ticket Number, ClientRequested, ClientSelectionForPortalVisibility,
    UAT status) stay on the child and are not copied up.
    """
    # Idempotent short-circuit: if a parent link already exists, skip.
    existing_parent = ado.get_parent_id(child_id)
    if existing_parent is not None:
        logger.info(
            "Child already has parent link — skipping mirror creation",
            extra={"ado_child_id": child_id, "existing_parent_id": existing_parent},
        )
        return

    work_item = ado.get_work_item(child_id)
    fields = work_item.get("fields", {}) or {}
    child_type = fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")

    # Resolve project from event or work item for backward compat
    if not project:
        project = fields.get(AdoFieldNames.TEAM_PROJECT, "") or settings.ado_project

    parent_type = AdoWorkItemTypes.get_parent_type(child_type)
    if not parent_type:
        logger.warning(
            "No parent type mapping for child — cannot mirror",
            extra={"ado_child_id": child_id, "child_type": child_type},
        )
        return

    # Two-phase copy:
    #   1. Create the parent with a minimal, universally safe field set.
    #      Different work item types (User Story, Bug, Epic, etc.) have
    #      different required/accepted fields, so starting minimal guarantees
    #      the create succeeds and the parent link can be established.
    #   2. Patch the remaining content fields one at a time, swallowing
    #      per-field errors. This way the worst case is "parent created
    #      and linked but missing AC/TS" instead of a DLQ message.
    # Parent types in this process have required custom fields
    # (Custom.ClientRequested and Custom.RequestCategory) — they must be set
    # at create time or the POST fails with TF401320. Both fields exist on the
    # child and we just copy them up.
    minimal_field_refs = [
        AdoFieldNames.TITLE,
        AdoFieldNames.DESCRIPTION,
        AdoFieldNames.PRIORITY,
        AdoFieldNames.AREA_PATH,
        AdoFieldNames.ITERATION_PATH,
        AdoFieldNames.CLIENT_REQUESTED,
        AdoFieldNames.REQUEST_CATEGORY,
    ]
    best_effort_field_refs = [
        AdoFieldNames.ACCEPTANCE_CRITERIA,
        AdoFieldNames.TEST_SCENARIOS,
        AdoFieldNames.UI_UX_ACCEPTANCE_CRITERIA,
    ]

    def _collect(refs: list[str]) -> dict:
        out: dict = {}
        for ref in refs:
            v = fields.get(ref)
            if v is not None and v != "":
                out[ref] = v
        return out

    minimal_fields = _collect(minimal_field_refs)
    if AdoFieldNames.TITLE not in minimal_fields:
        minimal_fields[AdoFieldNames.TITLE] = f"Parent for child {child_id}"

    try:
        created = ado.create_work_item(
            work_item_type=parent_type, fields=minimal_fields, project=project
        )
    except Exception as e:
        logger.error(
            "Failed to create mirrored parent with minimal fields — giving up",
            extra={
                "ado_child_id": child_id,
                "child_type": child_type,
                "parent_type": parent_type,
                "error": str(e)[:500],
            },
        )
        raise

    new_parent_id = created.get("id")
    if not new_parent_id:
        logger.error(
            "Mirrored parent creation returned no ID",
            extra={"ado_child_id": child_id, "parent_type": parent_type},
        )
        return

    # Link the child FIRST so the parent relationship is guaranteed even if
    # subsequent field patches partially fail.
    ado.add_parent_link(child_id=child_id, parent_id=int(new_parent_id), project=project)

    # Best-effort field copy — try each field individually so one rejected
    # field doesn't block the others.
    best_effort_fields = _collect(best_effort_field_refs)
    for ref, value in best_effort_fields.items():
        try:
            ado.update_work_item(int(new_parent_id), {ref: value})
        except Exception as e:
            logger.warning(
                "Skipping field on mirrored parent — rejected by ADO",
                extra={
                    "ado_parent_id": new_parent_id,
                    "parent_type": parent_type,
                    "field_ref": ref,
                    "error": str(e)[:300],
                },
            )

    # Best-effort attachment linking — ADO attachments live in a shared store,
    # so we just add relations on the parent pointing to the same URLs that
    # are already on the child (no download/re-upload needed).
    try:
        linked = ado.link_attachments_to_work_item(
            source_work_item_id=child_id,
            target_work_item_id=int(new_parent_id),
        )
        if linked:
            logger.info(
                "Linked attachments from child to mirrored parent",
                extra={
                    "ado_child_id": child_id,
                    "ado_parent_id": new_parent_id,
                    "attachments_linked": linked,
                },
            )
    except Exception as e:
        logger.warning(
            "Failed to link attachments to mirrored parent — continuing",
            extra={
                "ado_child_id": child_id,
                "ado_parent_id": new_parent_id,
                "error": str(e)[:300],
            },
        )

    logger.info(
        "Created and linked mirrored parent for HF-originated child",
        extra={
            "ado_child_id": child_id,
            "child_type": child_type,
            "new_parent_id": new_parent_id,
            "parent_type": parent_type,
            "action": "mirror_parent_created",
        },
    )


# Content field references used to detect description changes.
_CONTENT_FIELD_REFS = frozenset({
    AdoFieldNames.DESCRIPTION,
    AdoFieldNames.ACCEPTANCE_CRITERIA,
    AdoFieldNames.REPRO_STEPS,
    AdoFieldNames.TEST_SCENARIOS,
    AdoFieldNames.RELEASE_NOTES,
    AdoFieldNames.UI_UX_ACCEPTANCE_CRITERIA,
})


def _detect_content_change(
    ado: AdoService,
    hf: HappyFoxService,
    child_id: int,
    hf_ticket_id: int,
    changed_fields: list[str] | None,
    composed_text: str,
) -> bool:
    """Determine whether the composed description actually changed.

    Uses a three-tier strategy:
    1. Webhook changed_fields (fast path)
    2. ADO revision comparison
    3. HF description comparison (catches earlier unsynced changes)
    """
    # Check 1: changed_fields from the webhook.
    if changed_fields:
        if _CONTENT_FIELD_REFS & set(changed_fields):
            return True
        logger.info(
            "changed_fields present but no content fields — skipping description update",
            extra={"ado_child_id": child_id, "changed_fields": changed_fields},
        )
        return False

    # Check 2: inspect latest ADO revision for content changes.
    if ado.content_fields_changed(child_id):
        return True

    # Check 3: compare against what's currently on the HF ticket.
    current_hf_desc = hf.get_latest_description_html(hf_ticket_id)
    if _description_differs(composed_text, current_hf_desc):
        logger.info(
            "Description differs from HF — including in update",
            extra={"ado_child_id": child_id, "hf_ticket_id": hf_ticket_id},
        )
        return True

    return False


_MAX_INLINE_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MB — HappyFox per-update limit


def _process_inline_images(
    html: str,
    ado: AdoService,
    hf: HappyFoxService,
    hf_ticket_id: int,
) -> str:
    """
    Download ADO-embedded images, upload to HappyFox, rewrite ``<img>`` URLs.

    Extracts ``<img src="https://dev.azure.com/...">`` tags from the HTML,
    downloads each image from ADO (authenticated), uploads them all to the
    HappyFox ticket as attachments, then rewrites the ``src`` attributes
    to point to the HappyFox-hosted URLs.

    On per-image failure (download error, too large, etc.), the ``<img>``
    tag is replaced with a visible text notice so the customer knows an
    image was intended but could not be copied.

    Returns the rewritten HTML, or the original HTML if no ADO images found.
    """
    ado_images = extract_ado_image_urls(html)
    if not ado_images:
        return html

    logger.info(
        "Found ADO inline images to rewrite",
        extra={
            "hf_ticket_id": hf_ticket_id,
            "image_count": len(ado_images),
            "filenames": [img["filename"] for img in ado_images],
        },
    )

    # Download each image from ADO
    downloaded: list[tuple[str, bytes]] = []  # (filename, raw_bytes)
    url_to_filename: dict[str, str] = {}      # ado_url → filename
    failures: dict[str, str] = {}             # ado_url → reason

    for img_info in ado_images:
        ado_url = img_info["url"]
        filename = img_info["filename"]
        try:
            image_bytes = ado.download_attachment(ado_url)

            if len(image_bytes) > _MAX_INLINE_IMAGE_BYTES:
                reason = (
                    f"file too large ({len(image_bytes) / (1024 * 1024):.1f} MB, "
                    f"limit {_MAX_INLINE_IMAGE_BYTES // (1024 * 1024)} MB)"
                )
                logger.warning(
                    "Skipping oversized inline image",
                    extra={
                        "filename": filename,
                        "size_bytes": len(image_bytes),
                        "ado_url": ado_url,
                    },
                )
                failures[ado_url] = reason
                continue

            downloaded.append((filename, image_bytes))
            url_to_filename[ado_url] = filename
        except Exception as e:
            logger.warning(
                "Failed to download inline image from ADO",
                extra={"filename": filename, "ado_url": ado_url, "error": str(e)},
            )
            failures[ado_url] = f"download failed ({type(e).__name__})"

    if not downloaded and not failures:
        return html

    # Upload all successfully downloaded images to HappyFox in one call
    url_map: dict[str, str] = {}  # ado_url → hf_url
    if downloaded:
        try:
            filename_to_hf_url = hf.upload_inline_images(hf_ticket_id, downloaded)

            # Map back from ado_url → hf_url via the filename
            for ado_url, filename in url_to_filename.items():
                hf_url = filename_to_hf_url.get(filename)
                if hf_url:
                    url_map[ado_url] = hf_url
                else:
                    failures[ado_url] = "upload succeeded but URL not found in response"
        except Exception as e:
            logger.error(
                "Failed to upload inline images to HappyFox",
                extra={
                    "hf_ticket_id": hf_ticket_id,
                    "image_count": len(downloaded),
                    "error": str(e),
                },
            )
            # Mark all downloaded images as failed
            for ado_url in url_to_filename:
                if ado_url not in failures:
                    failures[ado_url] = f"upload failed ({type(e).__name__})"

    rewritten = rewrite_html_images(html, url_map, failures)

    logger.info(
        "Inline image rewriting complete",
        extra={
            "hf_ticket_id": hf_ticket_id,
            "rewritten": len(url_map),
            "failed": len(failures),
        },
    )
    return rewritten


def _update_hf_ticket(
    ado: AdoService,
    hf: HappyFoxService,
    event: IntegrationEvent,
    child_id: int,
    fields: dict,
    hf_ticket_id: int,
    child_work_item_type: str,
    client_name: str,
) -> None:
    """Build and send an update to an existing HappyFox ticket."""
    update_payload = ado_client_story_to_happyfox_update(
        fields=fields,
        hf_service=hf,
        child_work_item_type=child_work_item_type,
        hf_status_id=event.hf_status_id,
        project_id=event.project,
        changed_fields=event.changed_fields,
    )

    # Strip the description if content didn't actually change.
    # Exception: "Requirements Pending Acceptance" updates intentionally set
    # text to just the Acceptance Criteria — skip the content-change gate
    # for those since the AC wasn't necessarily edited in this event.
    _is_req_pending = (
        event.changed_fields
        and AdoFieldNames.REQUIREMENT_APPROVAL in event.changed_fields
        and fields.get(AdoFieldNames.REQUIREMENT_APPROVAL)
        == "Requirements Pending Acceptance"
    )
    if update_payload.text and not _is_req_pending:
        if not _detect_content_change(
            ado, hf, child_id, hf_ticket_id,
            event.changed_fields, update_payload.text,
        ):
            update_payload.text = None
            logger.info(
                "Description unchanged — omitted from HF update",
                extra={
                    "ado_child_id": child_id,
                    "hf_ticket_id": hf_ticket_id,
                    "changed_fields": event.changed_fields,
                },
            )

    # Rewrite ADO-embedded images in the description so customers can view
    # them. Downloads from ADO, uploads to HF, rewrites <img src> URLs.
    if update_payload.text:
        update_payload.text = _process_inline_images(
            update_payload.text, ado, hf, hf_ticket_id,
        )

    # Send only if there's something worth updating.
    has_changes = any([
        update_payload.text is not None,
        update_payload.priority_id is not None,
        update_payload.custom_fields,
        update_payload.subject is not None,
        update_payload.status_id is not None,
    ])

    if has_changes:
        hf.update_ticket(hf_ticket_id, update_payload)

        # Stamp the sync date on the child work item in ADO
        sync_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            ado.update_work_item(child_id, {AdoFieldNames.LATEST_SYNC_DATE: sync_ts})
        except Exception as e:
            logger.warning(
                "Failed to stamp LatestSyncDate after HF update — continuing",
                extra={"ado_child_id": child_id, "error": str(e)[:300]},
            )

        logger.info(
            "Updated existing HappyFox ticket",
            extra={
                "ado_child_id": child_id,
                "hf_ticket_id": hf_ticket_id,
                "client": client_name,
                "content_changed": update_payload.text is not None,
            },
        )
    else:
        logger.info(
            "Skipping HF update — no field changes detected",
            extra={
                "ado_child_id": child_id,
                "hf_ticket_id": hf_ticket_id,
                "client": client_name,
            },
        )


def _create_hf_ticket(
    ado: AdoService,
    hf: HappyFoxService,
    event: IntegrationEvent,
    child_id: int,
    fields: dict,
    parent_id: str,
    child_work_item_type: str,
    client_name: str,
) -> None:
    """Create a new HappyFox ticket and write the ID back to ADO."""
    # Resolve parent title for the HF "Parent" field
    parent_title = ""
    if parent_id:
        try:
            parent_wi = ado.get_work_item(int(parent_id))
            parent_title = parent_wi.get("fields", {}).get(AdoFieldNames.TITLE, "")
        except Exception as e:
            logger.warning(
                "Could not fetch parent title for HF ticket",
                extra={"parent_id": parent_id, "error": str(e)},
            )

    create_payload = ado_client_story_to_happyfox_create(
        fields=fields,
        hf_service=hf,
        category_id=settings.hf_default_category_id,
        ado_parent_title=parent_title,
        ado_child_id=str(child_id),
        child_work_item_type=child_work_item_type,
        project_id=event.project,
    )
    create_payload.email = settings.hf_create_user_email
    create_payload.name = settings.hf_create_user_name

    logger.info(
        "Creating HappyFox ticket",
        extra={
            "ado_child_id": child_id,
            "client": client_name,
            "category": settings.hf_default_category_id,
            "parent_id": parent_id,
        },
    )

    new_ticket = hf.create_ticket(create_payload)

    # Write the HF ticket ID, title, and sync timestamp back to the ADO Client Story
    sync_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ado.update_work_item(child_id, {
        AdoFieldNames.SUPPORT_TICKET_NUMBER: str(new_ticket.id),
        AdoFieldNames.SUPPORT_TICKET_TITLE: new_ticket.subject,
        AdoFieldNames.LATEST_SYNC_DATE: sync_ts,
    })

    # If the description contains ADO-embedded images, rewrite them now
    # that the HF ticket exists. Upload images as HF attachments, then
    # send a staff_update with the rewritten HTML so customers can view them.
    if create_payload.text and extract_ado_image_urls(create_payload.text):
        rewritten = _process_inline_images(
            create_payload.text, ado, hf, new_ticket.id,
        )
        from src.integration.models.happyfox_models import HappyFoxTicketUpdate
        image_update = HappyFoxTicketUpdate(text=rewritten)
        hf.update_ticket(new_ticket.id, image_update)
        logger.info(
            "Sent follow-up update with rewritten inline images",
            extra={
                "ado_child_id": child_id,
                "hf_ticket_id": new_ticket.id,
            },
        )

    logger.info(
        "Created new HappyFox ticket and linked to ADO",
        extra={
            "ado_child_id": child_id,
            "hf_ticket_id": new_ticket.id,
            "client": client_name,
        },
    )


def _process_event(event: IntegrationEvent) -> None:
    """Process a single child story event."""
    ado = AdoService(settings)
    hf = HappyFoxService(settings)

    try:
        child_id = int(event.resource_id)

        logger.info(
            "Processing child story event",
            extra={
                "ado_child_id": child_id,
                "event_type": event.event_type,
                "parent_id": event.parent_id,
                "client": event.client,
            },
        )

        # HF-originated child: create and link a mirrored parent.
        if event.event_type == EventType.CLIENT_STORY_NEEDS_PARENT:
            _mirror_parent_for_orphan_child(ado, child_id, project=event.project)
            return

        # Skip created events — HF ticket deferred until explicit sync trigger.
        if event.event_type == EventType.CLIENT_STORY_CREATED:
            logger.info(
                "Skipping HF sync for newly created child — waiting for explicit sync trigger",
                extra={"ado_child_id": child_id, "client": event.client},
            )
            return

        # Dedup: skip if recently processed.
        if _is_recently_processed(child_id):
            logger.info(
                "Skipping child event — already processed within dedup window",
                extra={
                    "ado_child_id": child_id,
                    "event_type": event.event_type,
                    "dedup_window_seconds": settings.child_dedup_window_seconds,
                },
            )
            return

        # Fetch child work item and resolve context.
        work_item = ado.get_work_item(child_id)
        fields = work_item.get("fields", {})
        child_work_item_type = fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")

        parent_id = event.parent_id or fields.get(AdoFieldNames.ADO_PARENT_ID, "")
        if not parent_id:
            resolved = ado.get_parent_id(child_id)
            parent_id = str(resolved) if resolved else ""
            if parent_id:
                logger.info(
                    "Resolved parent ID from ADO hierarchy relation",
                    extra={"ado_child_id": child_id, "parent_id": parent_id},
                )
        client_name = event.client or fields.get(AdoFieldNames.CLIENT_REQUESTED, "")

        # For "Sync Now" events, force a FULL sync (changed_fields = None)
        # because the user explicitly requested a complete resync. The webhook's
        # changed_fields only contains Custom.SyncToClientPortal, which isn't
        # useful for filtering — same pattern as parent_story_processor.
        if event.event_type == EventType.CLIENT_STORY_SYNC_NOW:
            event.changed_fields = None

        # Route to update or create.
        hf_ticket_id = _find_existing_hf_ticket(fields, hf, str(child_id))

        if hf_ticket_id is not None:
            _update_hf_ticket(
                ado, hf, event, child_id, fields,
                hf_ticket_id, child_work_item_type, client_name,
            )
        elif event.event_type == EventType.CLIENT_STORY_SYNC_NOW:
            # Only create HF tickets on explicit Sync Now events.
            # Field-update triggers (state change, UAT status, etc.) should
            # only UPDATE existing HF tickets — not create new ones. Children
            # that were created outside our integration flow (e.g., parent never
            # had Client Selection for Portal) should not get HF tickets from
            # a state change alone.
            _create_hf_ticket(
                ado, hf, event, child_id, fields,
                parent_id, child_work_item_type, client_name,
            )
        else:
            logger.info(
                "No existing HF ticket and event is not Sync Now — skipping creation",
                extra={
                    "ado_child_id": child_id,
                    "event_type": event.event_type,
                    "client": client_name,
                },
            )

        _mark_processed(child_id)

        # After a Child Sync Now event, reset the SyncToClientPortal field.
        if event.event_type == EventType.CLIENT_STORY_SYNC_NOW:
            ado.reset_sync_field(child_id, label="child")

    finally:
        ado.close()
        hf.close()


def _process_message(raw_body: str) -> None:
    """
    Parse and process a single Service Bus message.

    Raises on failure so the message gets abandoned and retried.
    """
    event = IntegrationEvent.model_validate_json(raw_body)
    _process_event(event)


def main(timer: func.TimerRequest) -> None:
    """
    Timer trigger entry point — polls the child events queue.

    Runs every 30 seconds. Processes up to 10 messages per invocation.
    Messages are only completed after successful processing — failed messages
    are abandoned and retried by Service Bus.
    """
    processed, failed = receive_and_process_from_queue(
        queue_name=settings.ado_child_events_queue,
        processor=_process_message,
        max_messages=10,
    )

    if processed or failed:
        logger.info(
            "Child story processor batch complete",
            extra={"processed": processed, "failed": failed},
        )
