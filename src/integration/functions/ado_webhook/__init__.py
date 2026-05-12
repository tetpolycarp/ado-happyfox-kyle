"""
ADO Webhook Receiver — HTTP trigger Azure Function.

Receives ADO Service Hook POST requests for workitem.created and workitem.updated
events, checks trigger conditions, and enqueues events to the appropriate
Service Bus queue.

Authentication: Currently relies on the Azure Function key (?code= parameter)
rather than HMAC signature validation. HMAC can be added later if needed.

Trigger conditions (values configurable via App Settings):

  workitem.created:
    1. Parent type created WITH Client Selection for Portal Syncing populated
       → parent queue (creates child work items per client)

  workitem.updated:
    2. Parent type state changes to ADO_TRIGGER_STATE_READY → parent queue
    3. Parent type Sync to Client Portal changes to ADO_TRIGGER_SYNC_NOW → parent queue
    4. Parent type Client Selection for Portal Syncing field changes → parent queue
       (new clients added / existing synced)
    5. Child type Sync to Client Portal changes to ADO_TRIGGER_SYNC_NOW → child queue
       (syncs individual child to its HappyFox ticket)
    6. Child type UAT Env Deployment Status changes (any value) → child queue
    7. Child type UAT Deployment Status changes (any value) → child queue
    8. Child type Requirements Acceptance Status changes (any value) → child queue
    9. Child type State changes (any value) → child queue

This handler is thin: validate → parse → check trigger → enqueue.
No API calls or heavy logic — that belongs in the queue processors.
"""

from __future__ import annotations

import json
import logging
from typing import NamedTuple

import azure.functions as func

from src.integration.config import settings
from src.integration.models.ado_models import (
    AdoFieldNames,
    AdoWebhookPayload,
    AdoWorkItemTypes,
)
from src.integration.models.events import EventType, IntegrationEvent
from src.integration.utils.logging_config import configure_logging

configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


class TriggerResult(NamedTuple):
    event_type: str | None
    target_queue: str | None


def _detect_trigger(payload: AdoWebhookPayload) -> TriggerResult:
    """
    Determine which trigger condition (if any) this webhook event matches.

    Returns:
        TriggerResult with (event_type, target_queue) or (None, None).
    """
    resource = payload.resource
    work_item_type = resource.get_work_item_type()

    # ---------------------------------------------------------------
    # workitem.created events
    # ---------------------------------------------------------------
    if payload.is_created:
        if AdoWorkItemTypes.is_parent_type(work_item_type):
            # Only create children if Client Selection for Portal Syncing has values.
            # If the field is empty at creation time, no children are created — they'll
            # be created later when the portal field is populated via an update event.
            portal_clients = resource.get_field(AdoFieldNames.CLIENT_SELECTION_PORTAL)
            if portal_clients and str(portal_clients).strip():
                logger.info(
                    "Trigger matched: parent created with portal clients",
                    extra={
                        "ado_id": resource.get_resource_id(),
                        "work_item_type": work_item_type,
                        "portal_clients": str(portal_clients),
                        "trigger": "parent_created_with_portal_clients",
                    },
                )
                return TriggerResult(EventType.USER_STORY_READY_FOR_DEV, settings.ado_parent_events_queue)

        # Child work item created via HappyFox automation — needs a mirrored parent.
        #
        # Detection: child type + Support Ticket Number populated at creation time.
        # Our own parent_story_processor creates children WITHOUT a Support Ticket
        # Number at creation (it writes the HF ID back AFTER the HF ticket is
        # created), so this filter cleanly distinguishes HF-originated children
        # from internally created ones. The downstream processor also verifies
        # no parent link already exists (idempotent short-circuit) before
        # creating the mirrored parent.
        if AdoWorkItemTypes.is_child_type(work_item_type):
            support_ticket_number = resource.get_field(AdoFieldNames.SUPPORT_TICKET_NUMBER)
            client_requested = resource.get_field(AdoFieldNames.CLIENT_REQUESTED)
            if support_ticket_number and client_requested:
                logger.info(
                    "Trigger matched: HF-originated child needs mirrored parent",
                    extra={
                        "ado_id": resource.get_resource_id(),
                        "work_item_type": work_item_type,
                        "support_ticket_number": support_ticket_number,
                        "client_requested": client_requested,
                        "trigger": "child_created_from_hf",
                    },
                )
                return TriggerResult(
                    EventType.CLIENT_STORY_NEEDS_PARENT,
                    settings.ado_child_events_queue,
                )

        # NOTE: Internally created child work items (from parent_story_processor)
        # are NOT handled here. They have no Support Ticket Number at create time
        # and the parent processor enqueues follow-up sync events itself. If we
        # also enqueued them here they'd be double-processed.

    # ---------------------------------------------------------------
    # workitem.updated events
    # ---------------------------------------------------------------
    elif payload.is_updated:
        changed = resource.get_changed_fields()

        if AdoWorkItemTypes.is_parent_type(work_item_type):
            # Trigger: State changed on parent work item.
            # "Ready for Development" gets the full sync treatment (content +
            # state + HF enqueue). Any other state change only propagates the
            # state to children in ADO — no HF sync.
            if changed.has_changed(AdoFieldNames.STATE):
                new_state = changed.new_value_for(AdoFieldNames.STATE)
                if new_state == settings.ado_trigger_state_ready:
                    logger.info(
                        "Trigger matched: %s → Ready for Development",
                        work_item_type,
                        extra={"ado_id": resource.get_resource_id(), "trigger": "state_change"},
                    )
                    return TriggerResult(EventType.USER_STORY_READY_FOR_DEV, settings.ado_parent_events_queue)
                else:
                    logger.info(
                        "Trigger matched: %s → State changed to '%s'",
                        work_item_type,
                        new_state,
                        extra={"ado_id": resource.get_resource_id(), "trigger": "parent_state_change"},
                    )
                    return TriggerResult(EventType.USER_STORY_STATE_CHANGED, settings.ado_parent_events_queue)

            # Trigger: Sync to Client Portal changed to configured sync value
            if changed.has_changed(AdoFieldNames.SYNC_TO_CLIENT_PORTAL):
                new_value = changed.new_value_for(AdoFieldNames.SYNC_TO_CLIENT_PORTAL)
                if new_value == settings.ado_trigger_sync_now:
                    logger.info(
                        "Trigger matched: %s → Sync Now",
                        work_item_type,
                        extra={"ado_id": resource.get_resource_id(), "trigger": "sync_now"},
                    )
                    return TriggerResult(EventType.USER_STORY_SYNC_NOW, settings.ado_parent_events_queue)

            # Trigger: Client Selection for Portal Syncing field changed
            # Routes to parent queue — parent_story_processor checks existing
            # children and only creates new ones for newly added clients.
            # Uses a distinct event type so the parent processor knows NOT to
            # enqueue newly created children for HF ticket creation — that only
            # happens on explicit Sync Now.
            if changed.has_changed(AdoFieldNames.CLIENT_SELECTION_PORTAL):
                logger.info(
                    "Trigger matched: %s → Client Selection for Portal Syncing changed",
                    work_item_type,
                    extra={
                        "ado_id": resource.get_resource_id(),
                        "trigger": "portal_clients_changed",
                        "new_value": changed.new_value_for(AdoFieldNames.CLIENT_SELECTION_PORTAL),
                    },
                )
                return TriggerResult(EventType.USER_STORY_PORTAL_CLIENTS_CHANGED, settings.ado_parent_events_queue)

        # Child work item triggers — direct child → HappyFox sync without
        # going through the parent processor.
        if AdoWorkItemTypes.is_child_type(work_item_type):
            # Trigger: Sync to Client Portal changed to "Sync Now" on a child.
            # Syncs this individual child ticket to its HappyFox mirror.
            if changed.has_changed(AdoFieldNames.SYNC_TO_CLIENT_PORTAL):
                new_value = changed.new_value_for(AdoFieldNames.SYNC_TO_CLIENT_PORTAL)
                if new_value == settings.ado_trigger_sync_now:
                    logger.info(
                        "Trigger matched: %s → Child Sync Now",
                        work_item_type,
                        extra={"ado_id": resource.get_resource_id(), "trigger": "child_sync_now"},
                    )
                    return TriggerResult(EventType.CLIENT_STORY_SYNC_NOW, settings.ado_child_events_queue)

            # Trigger: UAT Env Deployment Status changed (any value).
            # Syncs the updated UAT status custom field to HappyFox.
            if changed.has_changed(AdoFieldNames.UAT_ENV_DEPLOYMENT_STATUS):
                new_uat = changed.new_value_for(AdoFieldNames.UAT_ENV_DEPLOYMENT_STATUS)
                logger.info(
                    "Trigger matched: %s → UAT Env Deployment Status changed to '%s'",
                    work_item_type,
                    new_uat,
                    extra={"ado_id": resource.get_resource_id(), "trigger": "uat_env_field_update"},
                )
                return TriggerResult(EventType.CLIENT_STORY_UPDATED, settings.ado_child_events_queue)

            # Trigger: UAT Deployment Status changed (any value).
            # Also feeds into the same HappyFox UAT Status field (t-cf-41).
            if changed.has_changed(AdoFieldNames.UAT_DEPLOYMENT_STATUS):
                new_uat = changed.new_value_for(AdoFieldNames.UAT_DEPLOYMENT_STATUS)
                logger.info(
                    "Trigger matched: %s → UAT Deployment Status changed to '%s'",
                    work_item_type,
                    new_uat,
                    extra={"ado_id": resource.get_resource_id(), "trigger": "uat_deployment_field_update"},
                )
                return TriggerResult(EventType.CLIENT_STORY_UPDATED, settings.ado_child_events_queue)

            # Trigger: Requirements Acceptance Status changed (any value).
            # Syncs the updated requirements acceptance custom field to HappyFox.
            if changed.has_changed(AdoFieldNames.REQUIREMENT_APPROVAL):
                new_req = changed.new_value_for(AdoFieldNames.REQUIREMENT_APPROVAL)
                logger.info(
                    "Trigger matched: %s → Requirements Acceptance Status changed to '%s'",
                    work_item_type,
                    new_req,
                    extra={"ado_id": resource.get_resource_id(), "trigger": "req_acceptance_field_update"},
                )
                return TriggerResult(EventType.CLIENT_STORY_UPDATED, settings.ado_child_events_queue)

            # Trigger: State changed (any value).
            # Syncs the ADO state to the HappyFox "ADO Ticket State" text field.
            if changed.has_changed(AdoFieldNames.STATE):
                new_state = changed.new_value_for(AdoFieldNames.STATE)
                logger.info(
                    "Trigger matched: %s → State changed to '%s'",
                    work_item_type,
                    new_state,
                    extra={"ado_id": resource.get_resource_id(), "trigger": "state_field_update"},
                )
                return TriggerResult(EventType.CLIENT_STORY_UPDATED, settings.ado_child_events_queue)

            # Fallback trigger for HF-originated children that need a mirrored parent.
            #
            # The create-event trigger above only fires if the ADO subscription is
            # configured for workitem.created on Client types AND the Support Ticket
            # Number is populated at creation time. Neither is guaranteed: HF may
            # create the item first and patch the Support Ticket Number afterward,
            # and subscriptions for Client-type creates may not exist.
            #
            # This branch catches both cases by also firing on updated events when
            # Support Ticket Number is newly populated OR is present on a child
            # that currently has no parent link. The downstream processor's
            # idempotent parent-link check prevents double-processing.
            support_ticket_current = resource.get_field(AdoFieldNames.SUPPORT_TICKET_NUMBER)
            client_requested_current = resource.get_field(AdoFieldNames.CLIENT_REQUESTED)
            support_ticket_changed = changed.has_changed(AdoFieldNames.SUPPORT_TICKET_NUMBER)
            if support_ticket_current and client_requested_current and support_ticket_changed:
                new_value = changed.new_value_for(AdoFieldNames.SUPPORT_TICKET_NUMBER)
                old_value = changed.old_value_for(AdoFieldNames.SUPPORT_TICKET_NUMBER)
                # Only act when the value transitions from empty to populated —
                # an update from one HF ticket to another is not this trigger.
                if new_value and not old_value:
                    logger.info(
                        "Trigger matched: HF-originated child (Support Ticket Number populated via update)",
                        extra={
                            "ado_id": resource.get_resource_id(),
                            "work_item_type": work_item_type,
                            "support_ticket_number": new_value,
                            "client_requested": client_requested_current,
                            "trigger": "child_updated_from_hf",
                        },
                    )
                    return TriggerResult(
                        EventType.CLIENT_STORY_NEEDS_PARENT,
                        settings.ado_child_events_queue,
                    )

    return TriggerResult(None, None)


def main(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger entry point for ADO Service Hook webhooks.

    Parses payload → checks trigger → enqueues to correct queue via SDK.
    Auth is handled by the Azure Function key (?code= parameter).

    NOTE: Uses azure-servicebus SDK directly instead of Function output bindings
    because Flex Consumption plans fail to load functions with Service Bus bindings.
    """
    body = req.get_body()

    # 1. Parse the webhook payload
    try:
        raw = json.loads(body)
        payload = AdoWebhookPayload.model_validate(raw)
    except Exception as e:
        logger.error("Failed to parse ADO webhook payload", extra={"error": str(e)})
        return func.HttpResponse("Bad Request", status_code=400)

    # 2. Check trigger conditions
    event_type, target_queue = _detect_trigger(payload)

    if event_type is None:
        logger.debug(
            "No trigger matched for ADO webhook event",
            extra={
                "ado_id": payload.resource.get_resource_id(),
                "event_type": payload.event_type,
                "work_item_type": payload.resource.get_work_item_type(),
            },
        )
        return func.HttpResponse("No action required", status_code=204)

    # 3. Build the integration event
    resource = payload.resource
    # For updated events, capture which fields actually changed so downstream
    # processors can skip unchanged fields (e.g., don't repost description to
    # HappyFox when only Request Category changed).
    changed_field_refs: list[str] = []
    if payload.is_updated:
        changed = resource.get_changed_fields()
        changed_field_refs = [
            field_ref for field_ref in changed._fields.keys()
        ]

    event = IntegrationEvent(
        event_type=event_type,
        source="ado",
        resource_id=str(resource.get_resource_id()),
        project=payload.project_id or resource.get_field(AdoFieldNames.TEAM_PROJECT) or "",
        client=resource.get_field(AdoFieldNames.CLIENT_SELECTION_PORTAL) if payload.is_created else None,
        payload=raw,
        changed_fields=changed_field_refs,
    )

    # 4. Route to the correct queue via SDK
    try:
        from src.integration.services.servicebus_client import send_to_parent_queue, send_to_child_queue

        event_json = event.model_dump_json()
        if target_queue == settings.ado_parent_events_queue:
            send_to_parent_queue(event_json)
        else:
            send_to_child_queue(event_json)
    except Exception as e:
        logger.error(
            "Failed to enqueue event to Service Bus",
            extra={"error": str(e), "target_queue": target_queue},
        )
        return func.HttpResponse(f"Failed to enqueue: {e}", status_code=500)

    logger.info(
        "Enqueued ADO event",
        extra={
            "event_id": event.event_id,
            "event_type": event.event_type,
            "ado_id": event.resource_id,
            "target_queue": target_queue,
            "action": "enqueue",
        },
    )

    return func.HttpResponse("Accepted", status_code=202)
