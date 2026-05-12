"""Tests for mapping model serialization/deserialization."""

from __future__ import annotations

from src.integration.models.mapping_models import MappingRecord, SyncStatus


class TestMappingRecord:
    def test_to_table_entity(self):
        record = MappingRecord(
            ado_parent_id="100",
            client="Acme Corp",
            ado_child_id="200",
            hf_ticket_id="HF-999",
            sync_status=SyncStatus.SYNCED,
        )
        entity = record.to_table_entity()

        assert entity["PartitionKey"] == "100"
        assert entity["RowKey"] == "Acme Corp"
        assert entity["ado_child_id"] == "200"
        assert entity["hf_ticket_id"] == "HF-999"
        assert entity["sync_status"] == "synced"

    def test_from_table_entity(self):
        entity = {
            "PartitionKey": "100",
            "RowKey": "Acme Corp",
            "ado_child_id": "200",
            "hf_ticket_id": "HF-999",
            "sync_status": "synced",
            "created_at": "2026-03-30T12:00:00+00:00",
            "updated_at": "2026-03-30T13:00:00+00:00",
            "last_error": "",
        }
        record = MappingRecord.from_table_entity(entity)

        assert record.ado_parent_id == "100"
        assert record.client == "Acme Corp"
        assert record.ado_child_id == "200"
        assert record.hf_ticket_id == "HF-999"
        assert record.sync_status == SyncStatus.SYNCED
        assert record.last_error is None

    def test_roundtrip(self):
        original = MappingRecord(
            ado_parent_id="100",
            client="Beta Inc",
            ado_child_id="201",
        )
        entity = original.to_table_entity()
        restored = MappingRecord.from_table_entity(entity)

        assert restored.ado_parent_id == original.ado_parent_id
        assert restored.client == original.client
        assert restored.ado_child_id == original.ado_child_id
        assert restored.hf_ticket_id is None
        assert restored.sync_status == SyncStatus.PENDING

    def test_null_hf_ticket_id_serializes_as_empty_string(self):
        record = MappingRecord(
            ado_parent_id="100",
            client="Test",
            ado_child_id="200",
            hf_ticket_id=None,
        )
        entity = record.to_table_entity()
        assert entity["hf_ticket_id"] == ""
