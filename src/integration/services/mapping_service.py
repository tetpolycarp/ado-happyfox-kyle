"""
Ticket mapping CRUD via Azure Table Storage.

The mapping table tracks ado_parent_id ↔ ado_child_id ↔ hf_ticket_id ↔ client.
Uses azure-data-tables SDK with upsert (insert-or-merge) for idempotency.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from azure.data.tables import TableClient, UpdateMode

from src.integration.config import Settings
from src.integration.errors import MappingNotFoundError
from src.integration.models.mapping_models import MappingRecord, SyncStatus

logger = logging.getLogger(__name__)


class MappingService:
    """CRUD operations for the ticket-mapping Azure Table Storage table."""

    def __init__(self, settings: Settings) -> None:
        self._table_client = TableClient.from_connection_string(
            conn_str=settings.storage_connection_string.get_secret_value(),
            table_name=settings.mapping_table_name,
        )
        # Ensure table exists (idempotent)
        self._table_client.create_table()

    def get_by_parent_and_client(self, parent_id: str, client: str) -> MappingRecord | None:
        """
        Look up a mapping by parent ID + client name.

        Args:
            parent_id: The ADO parent User Story work item ID (PartitionKey).
            client: The client name (RowKey).

        Returns:
            MappingRecord if found, None otherwise.
        """
        try:
            entity = self._table_client.get_entity(
                partition_key=parent_id,
                row_key=client,
            )
            return MappingRecord.from_table_entity(entity)
        except Exception:
            # ResourceNotFoundError from azure SDK — entity doesn't exist
            return None

    def get_children_for_parent(self, parent_id: str) -> list[MappingRecord]:
        """
        Get all child mappings for a parent User Story.

        Args:
            parent_id: The ADO parent User Story work item ID (PartitionKey).

        Returns:
            List of MappingRecords for all children of this parent.
        """
        query = f"PartitionKey eq '{parent_id}'"
        entities = self._table_client.query_entities(query_filter=query)
        return [MappingRecord.from_table_entity(e) for e in entities]

    def get_by_child_id(self, child_id: str) -> MappingRecord | None:
        """
        Look up a mapping by child ADO work item ID.

        Note: This queries across all partitions (slower) since child_id
        is not a key. For frequent lookups, consider a secondary index.

        Args:
            child_id: The ADO child Client Story work item ID.

        Returns:
            MappingRecord if found, None otherwise.
        """
        query = f"ado_child_id eq '{child_id}'"
        entities = list(self._table_client.query_entities(query_filter=query, results_per_page=1))
        if entities:
            return MappingRecord.from_table_entity(entities[0])
        return None

    def put(self, record: MappingRecord) -> None:
        """
        Upsert a mapping record (insert or merge).

        Uses upsert with MERGE mode for idempotency — safe to call multiple times
        with the same record.

        Args:
            record: The mapping record to upsert.
        """
        record.updated_at = datetime.now(timezone.utc)
        entity = record.to_table_entity()

        self._table_client.upsert_entity(entity=entity, mode=UpdateMode.MERGE)

        logger.info(
            "Upserted mapping record",
            extra={
                "ado_parent_id": record.ado_parent_id,
                "ado_child_id": record.ado_child_id,
                "hf_ticket_id": record.hf_ticket_id,
                "client": record.client,
                "sync_status": record.sync_status,
                "action": "upsert_mapping",
            },
        )

    def update_hf_ticket_id(self, parent_id: str, client: str, hf_ticket_id: str) -> MappingRecord:
        """
        Update the HappyFox ticket ID on an existing mapping and mark as synced.

        Args:
            parent_id: The ADO parent User Story work item ID (PartitionKey).
            client: The client name (RowKey).
            hf_ticket_id: The newly created HappyFox ticket ID.

        Returns:
            The updated MappingRecord.

        Raises:
            MappingNotFoundError: If no mapping exists for this parent + client.
        """
        record = self.get_by_parent_and_client(parent_id, client)
        if record is None:
            raise MappingNotFoundError(
                f"No mapping found for parent={parent_id}, client={client}"
            )

        record.hf_ticket_id = hf_ticket_id
        record.sync_status = SyncStatus.SYNCED
        record.updated_at = datetime.now(timezone.utc)
        record.last_error = None

        self.put(record)
        return record

    def mark_error(self, parent_id: str, client: str, error_message: str) -> None:
        """Mark a mapping record as errored with a message."""
        record = self.get_by_parent_and_client(parent_id, client)
        if record is None:
            return  # Nothing to mark

        record.sync_status = SyncStatus.ERROR
        record.last_error = error_message[:500]  # Truncate long error messages
        record.updated_at = datetime.now(timezone.utc)
        self.put(record)

    def close(self) -> None:
        """Close the underlying table client."""
        self._table_client.close()
