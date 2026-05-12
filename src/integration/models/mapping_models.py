"""
Ticket mapping record schema for Azure Table Storage.

The mapping table tracks the relationship between ADO work items and HappyFox tickets.
Table Storage schema:
    PartitionKey = ado_parent_id (str) — groups all children of a parent together
    RowKey = client name — unique per parent + client combination
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class SyncStatus(StrEnum):
    """Status of the sync between ADO and HappyFox for a mapping record."""

    PENDING = "pending"  # Child story created, HF ticket not yet created
    SYNCED = "synced"  # HF ticket created and linked
    ERROR = "error"  # Sync failed — needs retry or manual intervention
    STALE = "stale"  # Sync hasn't completed within expected timeframe


class MappingRecord(BaseModel):
    """
    A single row in the ticket-mapping Azure Table Storage table.

    PartitionKey: ado_parent_id (groups all children of a User Story)
    RowKey: client name (unique per parent + client)
    """

    # Table Storage keys
    ado_parent_id: str  # Used as PartitionKey
    client: str  # Used as RowKey

    # IDs
    ado_child_id: str
    hf_ticket_id: str | None = None  # Null until HF ticket is created

    # Metadata
    sync_status: SyncStatus = SyncStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_error: str | None = None  # Last error message if sync_status == ERROR

    def to_table_entity(self) -> dict:
        """Convert to Azure Table Storage entity dict."""
        return {
            "PartitionKey": self.ado_parent_id,
            "RowKey": self.client,
            "ado_child_id": self.ado_child_id,
            "hf_ticket_id": self.hf_ticket_id or "",
            "sync_status": self.sync_status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_error": self.last_error or "",
        }

    @classmethod
    def from_table_entity(cls, entity: dict) -> MappingRecord:
        """Create a MappingRecord from an Azure Table Storage entity dict."""
        return cls(
            ado_parent_id=entity["PartitionKey"],
            client=entity["RowKey"],
            ado_child_id=entity["ado_child_id"],
            hf_ticket_id=entity.get("hf_ticket_id") or None,
            sync_status=SyncStatus(entity.get("sync_status", "pending")),
            created_at=datetime.fromisoformat(entity["created_at"]) if entity.get("created_at") else datetime.now(timezone.utc),
            updated_at=datetime.fromisoformat(entity["updated_at"]) if entity.get("updated_at") else datetime.now(timezone.utc),
            last_error=entity.get("last_error") or None,
        )
