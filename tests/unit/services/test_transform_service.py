"""Tests for the ADO → HappyFox field transformation logic."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.integration.models.ado_models import AdoFieldNames
from src.integration.services.transform_service import (
    ADO_PRIORITY_TO_HF_NAME,
    _compose_description,
    ado_client_story_to_happyfox_create,
    ado_client_story_to_happyfox_update,
)


@pytest.fixture
def mock_hf_service():
    """Mock HappyFox service with preset priority/status lookups."""
    svc = MagicMock()
    svc.get_priority_id_by_name.side_effect = lambda name: {
        "Urgent": 1, "High": 2, "Medium": 3, "Low": 4,
    }.get(name)
    # Status is now resolved by ID directly (settings.hf_default_status_id),
    # so no mock needed for get_status_id_by_name.
    svc.get_priorities.return_value = [MagicMock(id=3)]
    return svc


class TestComposeDescription:
    def test_all_sections_present(self):
        fields = {
            AdoFieldNames.DESCRIPTION: "<p>Desc</p>",
            AdoFieldNames.ACCEPTANCE_CRITERIA: "<p>AC</p>",
            AdoFieldNames.TEST_SCENARIOS: "Test 1",
            AdoFieldNames.UI_UX_ACCEPTANCE_CRITERIA: "<p>UI</p>",
        }
        result = _compose_description(fields)
        assert "Description" in result
        assert "Acceptance Criteria" in result
        assert "Test Scenarios" in result
        assert "UI and UX Acceptance Criteria" in result

    def test_empty_fields(self):
        result = _compose_description({})
        assert result == "(No description provided)"

    def test_partial_fields(self):
        fields = {AdoFieldNames.DESCRIPTION: "Only a description"}
        result = _compose_description(fields)
        assert "Description" in result
        assert "Acceptance Criteria" not in result


class TestAdoClientStoryToHappyfoxCreate:
    def test_basic_mapping(self, mock_hf_service):
        fields = {
            AdoFieldNames.TITLE: "Acme Corp - Feature X",
            AdoFieldNames.DESCRIPTION: "Description here",
            AdoFieldNames.PRIORITY: 2,
            AdoFieldNames.CLIENT_REQUESTED: "Acme Corp",
            AdoFieldNames.INTEGRATION_SPRINT: "Sprint 12",
            AdoFieldNames.RELEASE_VERSION: "v2.5.0",
        }

        result = ado_client_story_to_happyfox_create(
            fields=fields,
            hf_service=mock_hf_service,
            category_id=10,
            ado_parent_id="100",
            ado_child_id="200",
        )

        assert result.subject == "Acme Corp - Feature X"
        assert result.priority_id == 2  # "High" → ID 2
        assert result.status_id == 1  # "New" → ID 1
        assert result.category_id == 10
        assert result.custom_fields["c_cf_ado_parent_id"] == "100"
        assert result.custom_fields["c_cf_ado_child_id"] == "200"

    def test_priority_mapping_all_levels(self, mock_hf_service):
        for ado_priority, expected_name in ADO_PRIORITY_TO_HF_NAME.items():
            fields = {
                AdoFieldNames.TITLE: "Test",
                AdoFieldNames.PRIORITY: ado_priority,
            }
            result = ado_client_story_to_happyfox_create(
                fields=fields,
                hf_service=mock_hf_service,
                category_id=1,
                ado_parent_id="100",
                ado_child_id="200",
            )
            expected_id = mock_hf_service.get_priority_id_by_name(expected_name)
            assert result.priority_id == expected_id

    def test_unknown_priority_falls_back_to_medium(self, mock_hf_service):
        fields = {
            AdoFieldNames.TITLE: "Test",
            AdoFieldNames.PRIORITY: 99,
        }
        result = ado_client_story_to_happyfox_create(
            fields=fields,
            hf_service=mock_hf_service,
            category_id=1,
            ado_parent_id="100",
            ado_child_id="200",
        )
        # Falls back to "Medium" → ID 3
        assert result.priority_id == 3


class TestAdoClientStoryToHappyfoxUpdate:
    def test_priority_change(self, mock_hf_service):
        fields = {AdoFieldNames.PRIORITY: 1}
        result = ado_client_story_to_happyfox_update(fields=fields, hf_service=mock_hf_service)
        assert result.priority_id == 1  # "Urgent" → ID 1

    def test_description_change_triggers_text_update(self, mock_hf_service):
        fields = {AdoFieldNames.DESCRIPTION: "New description"}
        result = ado_client_story_to_happyfox_update(fields=fields, hf_service=mock_hf_service)
        assert result.text is not None
        assert "New description" in result.text

    def test_no_changes_returns_empty_update(self, mock_hf_service):
        result = ado_client_story_to_happyfox_update(fields={}, hf_service=mock_hf_service)
        assert result.subject is None
        assert result.text is None
        assert result.priority_id is None
