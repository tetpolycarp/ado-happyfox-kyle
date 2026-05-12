"""
HappyFox REST API request/response schemas.

IMPORTANT: HappyFox uses numeric IDs for status, priority, and category that are
environment-specific. These must be fetched at runtime via the API (GET /statuses/,
GET /priorities/, GET /categories/) and cached — never hardcoded.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class HappyFoxTicketCreate(BaseModel):
    """Payload for creating a new HappyFox ticket (POST /tickets/)."""

    subject: str
    text: str  # Description body (HTML or plain text)
    category_id: int = Field(alias="category")
    priority_id: int = Field(alias="priority")
    status_id: int = Field(alias="status")

    # Custom fields — sent at the top level of the API payload.
    # HappyFox ticket custom fields use "t-cf-<field_id>" format.
    # See transform_service.py for confirmed field IDs.
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    # Contact info (for ticket requester)
    email: str | None = None
    name: str | None = None

    model_config = {"populate_by_name": True}

    def to_api_payload(self) -> dict[str, Any]:
        """Convert to the dict format expected by the HappyFox API."""
        payload: dict[str, Any] = {
            "subject": self.subject,
            "text": self.text,
            "category": self.category_id,
            "priority": self.priority_id,
            "status": self.status_id,
        }
        if self.email:
            payload["email"] = self.email
        if self.name:
            payload["name"] = self.name

        # Merge custom fields at the top level
        payload.update(self.custom_fields)
        return payload


class HappyFoxTicketUpdate(BaseModel):
    """Payload for updating an existing HappyFox ticket (POST /ticket/{id}/staff-update/)."""

    subject: str | None = None
    text: str | None = None  # Adds a new message/note to the ticket
    priority_id: int | None = Field(default=None, alias="priority")
    status_id: int | None = Field(default=None, alias="status")
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    def to_api_payload(self) -> dict[str, Any]:
        """Convert to the dict format expected by the HappyFox API."""
        payload: dict[str, Any] = {}
        if self.subject is not None:
            payload["subject"] = self.subject
        if self.text is not None:
            payload["text"] = self.text
        if self.priority_id is not None:
            payload["priority"] = self.priority_id
        if self.status_id is not None:
            payload["status"] = self.status_id

        payload.update(self.custom_fields)
        return payload


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class HappyFoxStatusResponse(BaseModel):
    """A single status from GET /statuses/."""

    id: int
    name: str
    behavior: str | None = None  # e.g., "pending", "on hold"


class HappyFoxPriorityResponse(BaseModel):
    """A single priority from GET /priorities/."""

    id: int
    name: str


class HappyFoxCategoryResponse(BaseModel):
    """A single category from GET /categories/."""

    id: int
    name: str


class HappyFoxTicketResponse(BaseModel):
    """Response from GET /ticket/{id}/ or POST /tickets/."""

    id: int
    display_id: str | None = None
    subject: str
    status: HappyFoxStatusResponse | None = None
    priority: HappyFoxPriorityResponse | None = None
    category: HappyFoxCategoryResponse | None = None
    created_at: datetime | None = Field(default=None, alias="created_at")
    last_updated_at: datetime | None = Field(default=None, alias="last_updated_at")
    custom_fields: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
