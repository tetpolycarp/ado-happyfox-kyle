"""
Shared test fixtures for the ADO ↔ HappyFox integration test suite.

Provides mock settings and factory functions for unit and integration tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.integration.models.events import IntegrationEvent, EventType


# ---------------------------------------------------------------------------
# Mock settings (prevent real env var loading in tests)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def mock_settings():
    """Provide mock settings so tests don't need real env vars."""
    with patch("src.integration.config.Settings") as MockSettings:
        mock = MockSettings.return_value
        mock.service_bus_connection_string.get_secret_value.return_value = "mock-sb-conn"
        mock.ado_organization = "test-org"
        mock.ado_project = "test-project"
        mock.ado_webhook_secret.get_secret_value.return_value = "test-webhook-secret"
        mock.ado_base_url = "https://dev.azure.com/test-org/test-project/_apis"
        mock.happyfox_api_url = "https://test.happyfox.com/api/1.1/json"
        mock.happyfox_api_key.get_secret_value.return_value = "test-api-key"
        mock.happyfox_auth_code.get_secret_value.return_value = "test-auth-code"
        mock.happyfox_webhook_secret = None
        mock.ado_parent_events_queue = "ado-parent-events"
        mock.ado_child_events_queue = "ado-child-events"
        mock.log_level = "DEBUG"
        mock.environment = "test"
        yield mock


# ---------------------------------------------------------------------------
# Factory functions for test data
# ---------------------------------------------------------------------------

def make_integration_event(
    *,
    event_type: str = EventType.USER_STORY_READY_FOR_DEV,
    resource_id: str = "12345",
    parent_id: str | None = None,
    client: str | None = None,
    payload: dict | None = None,
) -> IntegrationEvent:
    """Create an IntegrationEvent with sensible defaults for testing."""
    return IntegrationEvent(
        event_type=event_type,
        source="ado",
        resource_id=resource_id,
        parent_id=parent_id,
        client=client,
        payload=payload or {},
    )
