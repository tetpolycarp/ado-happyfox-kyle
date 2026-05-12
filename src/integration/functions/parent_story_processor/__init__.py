"""
Parent Story Processor — Timer-triggered Azure Function.

Polls the ado-parent-events queue via SDK. When a parent work item triggers
(portal clients populated, Ready for Development, or Sync Now), this function:

1. Fetches the full parent work item from ADO
2. Determines the correct child type based on the parent type
   (Epic→Client Epic, User Story→Client Story, Bug→Client Bug, etc.)
3. Extracts the Client Selection for Portal Syncing field (semicolon-separated)
4. Checks existing child work items via ADO parent-child relations
5. Creates a child work item for each client (if not already existing)
6. Sends one event per child to ado-child-events for HappyFox sync

NOTE: Uses timer trigger + SDK polling instead of Service Bus trigger bindings
because Flex Consumption plans fail to load functions with Service Bus bindings.
"""

from __future__ import annotations

import logging

import azure.functions as func

from src.integration.config import settings
from src.integration.models.ado_models import AdoFieldNames, AdoWorkItemTypes
from src.integration.models.events import EventType, IntegrationEvent
from src.integration.services.ado_service import AdoService
from src.integration.services.servicebus_client import (
    receive_and_process_from_queue,
    send_to_child_queue,
)
from src.integration.utils.logging_config import configure_logging

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


def _get_existing_children_by_client(
    ado: AdoService, parent_id: int, expected_child_type: str,
) -> dict[str, int]:
    """
    Fetch existing child work items of the expected type and return a map of client -> child ID.

    Indexes by both CLIENT_REQUESTED and CLIENT_SELECTION_PORTAL (lowercased)
    so that matching works whether the child was created before or after the
    portal field was introduced.
    """
    children = ado.get_child_work_items(parent_id)
    client_to_child: dict[str, int] = {}

    for child in children:
        child_fields = child.get("fields", {})
        child_type = child_fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")

        if child_type != expected_child_type:
            continue

        child_id = child["id"]

        # Index by CLIENT_REQUESTED (original field)
        client_name = child_fields.get(AdoFieldNames.CLIENT_REQUESTED, "")
        if client_name:
            client_to_child[client_name.lower()] = child_id

        # Also index by portal field so matching works with full names
        portal_name = child_fields.get(AdoFieldNames.CLIENT_SELECTION_PORTAL, "")
        if portal_name:
            client_to_child[portal_name.lower()] = child_id

    return client_to_child


def _process_event(event: IntegrationEvent) -> None:
    """Process a single parent story event."""
    ado = AdoService(settings)

    try:
        parent_id = int(event.resource_id)

        # Fetch the full parent work item from ADO
        work_item = ado.get_work_item(parent_id)
        fields = work_item.get("fields", {})

        # Resolve the ADO project name — prefer event (from webhook payload),
        # fall back to the fetched work item's System.TeamProject.
        project_name = (
            event.project
            or fields.get(AdoFieldNames.TEAM_PROJECT, "")
            or settings.ado_project
        )

        # Determine parent type and the correct child type to create
        parent_type = fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")
        child_type = AdoWorkItemTypes.get_child_type(parent_type)
        if not child_type:
            logger.warning(
                "Unknown parent work item type — cannot determine child type",
                extra={"ado_id": parent_id, "parent_type": parent_type},
            )
            return

        # --- State-only sync (ADO only, no HF) ---
        # When a parent's state changes (not Ready for Dev), propagate the
        # state to all children in ADO. No content sync, no HF enqueue.
        # Uses ADO relations directly — doesn't require portal client list.
        if event.event_type == EventType.USER_STORY_STATE_CHANGED:
            parent_state = fields.get(AdoFieldNames.STATE, "")
            if not parent_state:
                logger.warning(
                    "Parent state is empty — skipping state sync",
                    extra={"ado_id": parent_id},
                )
                return

            child_state_map = settings.get_state_to_child_state_map(parent_type)
            if child_state_map:
                mapped_state = child_state_map.get(parent_state)
            else:
                mapped_state = parent_state

            if not mapped_state:
                logger.info(
                    "No child state mapping for parent state — skipping",
                    extra={"ado_id": parent_id, "parent_state": parent_state},
                )
                return

            children = ado.get_child_work_items(parent_id)
            updated_count = 0
            skipped_type = 0
            for child in children:
                child_id = child["id"]
                child_fields = child.get("fields", {})

                # Only sync state to children that match the expected client
                # work item type (e.g. Client Bug for Bug parents).  Other
                # child types (Task, Test Case, etc.) may not share the same
                # workflow states and would cause ADO 400 errors + wasted
                # retries that risk Service Bus lock expiration.
                actual_type = child_fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")
                if child_type and actual_type != child_type:
                    skipped_type += 1
                    continue

                current_state = child_fields.get(AdoFieldNames.STATE, "")
                if current_state == mapped_state:
                    continue  # Already matches — no-op
                try:
                    ado.update_work_item(child_id, {AdoFieldNames.STATE: mapped_state})
                    updated_count += 1
                    logger.info(
                        "Synced parent state to child",
                        extra={
                            "ado_parent_id": parent_id,
                            "ado_child_id": child_id,
                            "parent_state": parent_state,
                            "child_state": mapped_state,
                        },
                    )
                except Exception as e:
                    logger.error(
                        "Failed to sync state to child — continuing",
                        extra={
                            "ado_parent_id": parent_id,
                            "ado_child_id": child_id,
                            "target_state": mapped_state,
                            "error": str(e)[:500],
                        },
                    )

            logger.info(
                "Parent state sync complete",
                extra={
                    "ado_parent_id": parent_id,
                    "parent_state": parent_state,
                    "mapped_child_state": mapped_state,
                    "children_found": len(children),
                    "children_updated": updated_count,
                    "skipped_wrong_type": skipped_type,
                },
            )
            return

        # Track which fields the webhook reported as changed on the parent.
        # Used to (a) only sync changed fields to children and (b) tell the
        # child processor which fields to push to HappyFox.
        #
        # For "Sync Now" events, always do a FULL sync (parent_changed = None)
        # because the user explicitly requested a complete resync. The webhook's
        # changed_fields would only contain Custom.SyncToClientPortal, which isn't
        # useful for filtering.
        if event.event_type in (EventType.USER_STORY_SYNC_NOW, EventType.USER_STORY_PORTAL_CLIENTS_CHANGED):
            parent_changed = None
        else:
            parent_changed = set(event.changed_fields) if event.changed_fields else None

        # Extract Client Selection for Portal Syncing (semicolon-delimited).
        # This is the authoritative list of which clients get child work items.
        # Values are 1:1 with HappyFox "Client Requested" choice text.
        portal_raw = fields.get(AdoFieldNames.CLIENT_SELECTION_PORTAL, "")
        if isinstance(portal_raw, str):
            clients = [c.strip() for c in portal_raw.split(";") if c.strip()]
        elif isinstance(portal_raw, list):
            clients = [str(c).strip() for c in portal_raw if str(c).strip()]
        else:
            clients = []

        if not clients:
            logger.warning(
                "No clients found in Client Selection for Portal Syncing",
                extra={"ado_id": parent_id, "parent_type": parent_type},
            )
            return

        logger.info(
            "Found clients for child work item creation",
            extra={
                "ado_id": parent_id,
                "parent_type": parent_type,
                "child_type": child_type,
                "client_count": len(clients),
                "clients": clients,
            },
        )

        # Check which children already exist via ADO relations
        existing_children = _get_existing_children_by_client(ado, parent_id, child_type)

        # Determine if this is a sync event (push parent fields to children)
        is_sync = event.event_type in (
            EventType.USER_STORY_SYNC_NOW,
            EventType.USER_STORY_READY_FOR_DEV,
            EventType.USER_STORY_PORTAL_CLIENTS_CHANGED,
        )

        # --- State mapping (applied during Sync Now / Ready for Dev) ---
        # Look up the parent ADO state → child ADO state and → HF status ID
        # so both propagate in a single sync cycle.
        parent_state = fields.get(AdoFieldNames.STATE, "")
        child_state_map = settings.get_state_to_child_state_map(parent_type)
        hf_status_map = settings.get_state_to_hf_status_map(parent_type)
        mapped_child_state: str | None = None
        mapped_hf_status: int | None = None
        if is_sync and parent_state:
            # For types with a non-1:1 mapping (Epic, Initiative, Feature),
            # look up the corresponding child state. For 1:1 types (User Story,
            # Bug), the map is empty and we copy the parent state directly.
            if child_state_map:
                mapped_child_state = child_state_map.get(parent_state)
            else:
                mapped_child_state = parent_state
            mapped_hf_status = hf_status_map.get(parent_state)

        # Create child stories and enqueue events.
        # Client names come from the portal field, so client_name IS the
        # portal-canonical name (1:1 with HappyFox choice text).
        for client_name in clients:
            portal_client_name = client_name
            existing_child_id = existing_children.get(client_name.lower())

            if existing_child_id is not None:
                # If this is a Sync Now / Ready for Dev event, push parent
                # field updates down to the existing child in ADO first.
                #
                # Capture the fields that were actually written (after diffing
                # against the child's current values) so we can tell the child
                # processor exactly what changed — avoids relying on the ADO
                # revision API which may return stale data.
                actually_written_fields: list[str] = []
                if is_sync:
                    try:
                        sync_result = ado.sync_child_from_parent(
                            child_id=existing_child_id,
                            client_name=client_name,
                            parent_fields=fields,
                            portal_client_name=portal_client_name,
                            only_fields=parent_changed,
                            child_state=mapped_child_state,
                        )
                        actually_written_fields = sync_result.get(
                            "_actually_written_fields", []
                        )
                        logger.info(
                            "Synced parent fields to existing child",
                            extra={
                                "ado_parent_id": parent_id,
                                "ado_child_id": existing_child_id,
                                "client": client_name,
                                "only_fields": list(parent_changed) if parent_changed else "all",
                                "actually_written": actually_written_fields,
                                "child_state": mapped_child_state,
                            },
                        )
                    except Exception as e:
                        logger.error(
                            "Failed to sync parent fields to child — continuing with HF sync",
                            extra={
                                "ado_parent_id": parent_id,
                                "ado_child_id": existing_child_id,
                                "client": client_name,
                                "error": str(e),
                            },
                        )

                # Use the actually-written fields if we have them (accurate);
                # fall back to parent_changed (best guess from webhook).
                child_changed = (
                    actually_written_fields
                    if actually_written_fields
                    else (list(parent_changed) if parent_changed else [])
                )

                # Portal client changes are ADO-only — sync fields to
                # existing children but do NOT enqueue for HappyFox.
                if event.event_type == EventType.USER_STORY_PORTAL_CLIENTS_CHANGED:
                    logger.info(
                        "Child story already exists — ADO sync only (portal client change)",
                        extra={
                            "ado_parent_id": parent_id,
                            "ado_child_id": existing_child_id,
                            "client": client_name,
                        },
                    )
                    continue

                logger.info(
                    "Child story already exists — enqueuing for HF sync",
                    extra={
                        "ado_parent_id": parent_id,
                        "ado_child_id": existing_child_id,
                        "client": client_name,
                    },
                )
                # If the parent trigger was Sync Now, propagate that to
                # existing children so the child processor creates HF
                # tickets for children that don't have them yet.
                child_event_type = (
                    EventType.CLIENT_STORY_SYNC_NOW
                    if event.event_type == EventType.USER_STORY_SYNC_NOW
                    else EventType.CLIENT_STORY_UPDATED
                )
                child_event = IntegrationEvent(
                    event_type=child_event_type,
                    source="ado",
                    resource_id=str(existing_child_id),
                    parent_id=str(parent_id),
                    project=project_name,
                    client=client_name,
                    changed_fields=child_changed,
                    hf_status_id=mapped_hf_status,
                )
                send_to_child_queue(child_event.model_dump_json())
                continue

            try:
                child_result = ado.create_child_work_item(
                    parent_id=parent_id,
                    child_type=child_type,
                    client_name=client_name,
                    parent_fields=fields,
                    portal_client_name=portal_client_name,
                    project=project_name,
                )
                child_id = str(child_result["id"])

                # For Sync Now events, enqueue the newly created child for
                # HF ticket creation immediately — the user explicitly
                # requested a sync, so the child should get an HF ticket.
                # For other triggers (Ready for Dev, portal field change),
                # defer HF creation until a future Sync Now.
                if event.event_type == EventType.USER_STORY_SYNC_NOW:
                    child_event = IntegrationEvent(
                        event_type=EventType.CLIENT_STORY_SYNC_NOW,
                        source="ado",
                        resource_id=child_id,
                        parent_id=str(parent_id),
                        project=project_name,
                        client=client_name,
                        hf_status_id=mapped_hf_status,
                    )
                    send_to_child_queue(child_event.model_dump_json())
                    logger.info(
                        "Created child work item and enqueued for HF sync (Sync Now)",
                        extra={
                            "ado_parent_id": parent_id,
                            "ado_child_id": child_id,
                            "child_type": child_type,
                            "client": client_name,
                        },
                    )
                else:
                    logger.info(
                        "Created child work item (HF sync deferred until explicit trigger)",
                        extra={
                            "ado_parent_id": parent_id,
                            "ado_child_id": child_id,
                            "child_type": child_type,
                            "client": client_name,
                        },
                    )
            except Exception as e:
                logger.error(
                    "Failed to create child work item",
                    extra={
                        "ado_parent_id": parent_id,
                        "child_type": child_type,
                        "client": client_name,
                        "error": str(e),
                    },
                )

        # After a Sync Now event has been processed, reset the SyncToClientPortal
        # field on the parent so the picklist returns to its idle state and the
        # user can trigger another sync without having to toggle away and back.
        # The reset value ("None" by default) is NOT a trigger, so this update
        # produces a no-op webhook — no re-processing loop.
        if event.event_type == EventType.USER_STORY_SYNC_NOW:
            ado.reset_sync_field(parent_id, label="parent")

    finally:
        ado.close()


def _process_message(raw_body: str) -> None:
    """
    Parse and process a single Service Bus message.

    Raises on failure so the message gets abandoned and retried.
    """
    event = IntegrationEvent.model_validate_json(raw_body)
    _process_event(event)


def main(timer: func.TimerRequest) -> None:
    """
    Timer trigger entry point — polls the parent events queue.

    Runs every 30 seconds. Processes up to 10 messages per invocation.
    Messages are only completed after successful processing — failed messages
    are abandoned and retried by Service Bus.
    """
    processed, failed = receive_and_process_from_queue(
        queue_name=settings.ado_parent_events_queue,
        processor=_process_message,
        max_messages=10,
    )

    if processed or failed:
        logger.info(
            "Parent story processor batch complete",
            extra={"processed": processed, "failed": failed},
        )
