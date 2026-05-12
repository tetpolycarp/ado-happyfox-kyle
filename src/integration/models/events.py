"""
Internal event envelope — the message schema for Service Bus queues.

Every message on the bus is a JSON-serialized IntegrationEvent.
The event_id (UUID) is used for dedup checks in the Sync Processor.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """Known event types flowing through the integration."""

    # User Story triggers → ado-parent-events queue
    USER_STORY_READY_FOR_DEV = "ado.userstory.ready_for_dev"
    USER_STORY_SYNC_NOW = "ado.userstory.sync_now"
    USER_STORY_STATE_CHANGED = "ado.userstory.state_changed"
    USER_STORY_PORTAL_CLIENTS_CHANGED = "ado.userstory.portal_clients_changed"

    # Client Story events → ado-child-events queue
    CLIENT_STORY_CREATED = "ado.clientstory.created"
    CLIENT_STORY_UPDATED = "ado.clientstory.updated"
    CLIENT_STORY_SYNC_NOW = "ado.clientstory.sync_now"
    CLIENT_STORY_UAT_SYNC = "ado.clientstory.uat_sync"
    # HappyFox-originated child with no parent link — needs a mirrored parent created/linked
    CLIENT_STORY_NEEDS_PARENT = "ado.clientstory.needs_parent"


class IntegrationEvent(BaseModel):
    """
    Message envelope for Service Bus queues.

    Fields:
        event_id: UUID for dedup — generated at enqueue time.
        event_type: Classifies the trigger that produced this event.
        source: Which system originated the event.
        resource_id: ADO work item ID (str) of the triggering item.
        parent_id: ADO work item ID of the parent User Story (if applicable).
        client: Client name from the Clients multi-select (if applicable).
        timestamp: When the event was created (UTC).
        payload: Raw parsed payload from the originating system.
    """

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    source: Literal["ado", "happyfox"]
    resource_id: str
    parent_id: str | None = None
    client: str | None = None
    # ADO project name (System.TeamProject) — used to scope API calls to the
    # correct project. Populated from the webhook payload.
    project: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
    # When True, child processor should only sync attachments — skip the
    # staff_update message for description/AC/test scenarios. Used by Sync Now
    # when the parent processor already pushed field updates to the ADO child.
    attachments_only: bool = False
    # ADO field reference names that actually changed in this event. When
    # populated, the child processor uses this to decide whether to include
    # description in the HF update. Empty list = unknown / assume all changed.
    changed_fields: list[str] = Field(default_factory=list)
    # HappyFox status ID (numeric) to set on the ticket during this sync.
    # Used directly — no runtime name→ID lookup needed. Set by the parent
    # processor when propagating state changes — the mapping is driven by
    # the *parent* ADO state, not the child's, because the same child state
    # can map to different HF statuses depending on which parent state
    # triggered the sync.
    hf_status_id: int | None = None
