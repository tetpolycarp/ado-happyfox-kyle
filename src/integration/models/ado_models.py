"""
ADO webhook payload shapes and work item field schemas.

ADO Service Hook payloads for workitem.updated events follow a specific structure.
Custom field reference names use the project's namespace — these MUST be confirmed
against your actual ADO process template before deployment.

NOTE: ADO field reference names are namespaced (e.g., "System.Title", "Custom.ClientRequested").
Always use the reference name, never the display name, in code.

Custom field references and trigger values are loaded from Azure App Settings via
config.py so they can be changed without redeploying code. Standard "System.*" and
"Microsoft.*" fields are hardcoded — they never change across ADO instances.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ADO field reference name constants
#
# Standard (System.*, Microsoft.*) fields are hardcoded — they are the same
# across all ADO organizations. Custom.* fields are loaded from App Settings
# at startup so they can be adjusted if your ADO process template changes.
# ---------------------------------------------------------------------------

# Lazy import to avoid circular dependency at module level.
# settings is accessed once at import time to populate the class attributes.
from src.integration.config import settings as _settings


class AdoFieldNames:
    """
    ADO field reference name constants.

    Standard fields are hardcoded. Custom fields are loaded from centralized
    settings (Azure App Settings) so they can be changed without redeploying.
    Confirmed against live webhook payloads captured 2026-04-08.
    """

    # Standard fields (hardcoded — same across all ADO orgs)
    TITLE = "System.Title"
    DESCRIPTION = "System.Description"
    STATE = "System.State"
    REASON = "System.Reason"
    WORK_ITEM_TYPE = "System.WorkItemType"
    ASSIGNED_TO = "System.AssignedTo"
    AREA_PATH = "System.AreaPath"
    ITERATION_PATH = "System.IterationPath"
    TEAM_PROJECT = "System.TeamProject"
    CREATED_DATE = "System.CreatedDate"
    CREATED_BY = "System.CreatedBy"
    CHANGED_DATE = "System.ChangedDate"
    CHANGED_BY = "System.ChangedBy"
    COMMENT_COUNT = "System.CommentCount"
    BOARD_COLUMN = "System.BoardColumn"  # Parent (User Story) only

    # Standard extended fields (hardcoded)
    PRIORITY = "Microsoft.VSTS.Common.Priority"
    SEVERITY = "Microsoft.VSTS.Common.Severity"  # Bug only
    ACCEPTANCE_CRITERIA = "Microsoft.VSTS.Common.AcceptanceCriteria"
    REPRO_STEPS = "Microsoft.VSTS.TCM.ReproSteps"  # Bug only
    STATE_CHANGE_DATE = "Microsoft.VSTS.Common.StateChangeDate"
    VALUE_AREA = "Microsoft.VSTS.Common.ValueArea"  # Parent only
    STACK_RANK = "Microsoft.VSTS.Common.StackRank"

    # --- Custom fields (loaded from App Settings) ---

    # Shared between parent and child work items
    CLIENT_REQUESTED = _settings.ado_field_client_requested
    CLIENT_SELECTION_PORTAL = _settings.ado_field_client_selection_portal
    REQUEST_CATEGORY = _settings.ado_field_request_category
    TEST_SCENARIOS = _settings.ado_field_test_scenarios
    UAT_FEEDBACK_RESOLVED = _settings.ado_field_uat_feedback_resolved

    # Parent work items only
    UI_UX_ACCEPTANCE_CRITERIA = _settings.ado_field_ui_ux_acceptance_criteria
    IS_NEW_FUNCTIONALITY = _settings.ado_field_is_new_functionality
    SYNC_TO_CLIENT_PORTAL = _settings.ado_field_sync_to_client_portal
    PRODUCTION_RELEASE_DATE = _settings.ado_field_production_release_date
    RELEASE_VERSION = _settings.ado_field_release_version
    INTEGRATION_SPRINT = _settings.ado_field_integration_sprint

    # Shared content field (parent and child)
    RELEASE_NOTES = _settings.ado_field_release_notes

    # Child work items only
    SCRUM_TEAM = _settings.ado_field_scrum_team
    UAT_DEPLOYMENT_STATUS = _settings.ado_field_uat_deployment_status
    UAT_ENV_DEPLOYMENT_STATUS = _settings.ado_field_uat_env_deployment_status
    SUPPORT_TICKET_NUMBER = _settings.ado_field_support_ticket_number
    SUPPORT_TICKET_STATUS = _settings.ado_field_support_ticket_status
    SUPPORT_TICKET_TITLE = _settings.ado_field_support_ticket_title
    ADO_PARENT_ID = _settings.ado_field_ado_parent_id
    REQUIREMENT_APPROVAL = _settings.ado_field_requirements_approval_status
    RELEASE_APPROVAL = _settings.ado_field_release_approval
    CONTRACT_REQUIREMENT_NUMBERS = _settings.ado_field_contract_requirement_numbers
    LATEST_SYNC_DATE = _settings.ado_field_latest_sync_date


# ---------------------------------------------------------------------------
# Work item type constants
# ---------------------------------------------------------------------------

class AdoWorkItemTypes:
    """ADO work item type names."""

    # Parent types (triggers child creation)
    USER_STORY = "User Story"
    ISSUE = "Issue"
    EPIC = "Epic"
    BUG = "Bug"
    INITIATIVE = "Initiative"
    FEATURE = "Feature"

    # Child types (created per client, synced to HappyFox)
    CLIENT_STORY = "Client Story Work Item"
    CLIENT_EPIC = "Client Epic Work Item"
    CLIENT_BUG = "Client Bug Work Item"
    CLIENT_INITIATIVE = "Client Initiative Work Item"
    CLIENT_FEATURE = "Client Feature Work Item"

    # Parent type → child type mapping
    PARENT_TO_CHILD: dict[str, str] = {
        USER_STORY: CLIENT_STORY,
        ISSUE: CLIENT_STORY,  # Issue → same child type as User Story
        EPIC: CLIENT_EPIC,
        BUG: CLIENT_BUG,
        INITIATIVE: CLIENT_INITIATIVE,
        FEATURE: CLIENT_FEATURE,
    }

    # Child type → HappyFox "Issue Type - Development" value
    CHILD_TO_HF_ISSUE_TYPE: dict[str, str] = {
        CLIENT_STORY: "Story",
        CLIENT_EPIC: "Epic",
        CLIENT_BUG: "Bug",
        CLIENT_INITIATIVE: "Initiative",
        CLIENT_FEATURE: "Feature",
    }

    # All parent types (for trigger matching)
    ALL_PARENT_TYPES: set[str] = {USER_STORY, ISSUE, EPIC, BUG, INITIATIVE, FEATURE}

    # All child types (for trigger matching)
    ALL_CHILD_TYPES: set[str] = {CLIENT_STORY, CLIENT_EPIC, CLIENT_BUG, CLIENT_INITIATIVE, CLIENT_FEATURE}

    # Child type → parent type mapping (reverse lookup for HF-originated children)
    CHILD_TO_PARENT: dict[str, str] = {
        CLIENT_STORY: USER_STORY,
        CLIENT_EPIC: EPIC,
        CLIENT_BUG: BUG,
        CLIENT_INITIATIVE: INITIATIVE,
        CLIENT_FEATURE: FEATURE,
    }

    @classmethod
    def get_child_type(cls, parent_type: str) -> str | None:
        """Get the child work item type for a given parent type."""
        return cls.PARENT_TO_CHILD.get(parent_type)

    @classmethod
    def get_parent_type(cls, child_type: str) -> str | None:
        """Get the parent work item type for a given child type."""
        return cls.CHILD_TO_PARENT.get(child_type)

    @classmethod
    def get_hf_issue_type(cls, child_type: str) -> str | None:
        """Get the HappyFox Issue Type value for a given child work item type."""
        return cls.CHILD_TO_HF_ISSUE_TYPE.get(child_type)

    @classmethod
    def is_parent_type(cls, work_item_type: str) -> bool:
        """Check if a work item type is a parent type."""
        return work_item_type in cls.ALL_PARENT_TYPES

    @classmethod
    def is_child_type(cls, work_item_type: str) -> bool:
        """Check if a work item type is a child type."""
        return work_item_type in cls.ALL_CHILD_TYPES


# ---------------------------------------------------------------------------
# Webhook payload models
# ---------------------------------------------------------------------------

class AdoWebhookFieldChange(BaseModel):
    """A single field change record from a workitem.updated event."""

    old_value: Any = Field(default=None, alias="oldValue")
    new_value: Any = Field(default=None, alias="newValue")

    model_config = {"populate_by_name": True}


class AdoWebhookChangedFields:
    """
    Helper for accessing changed fields from a workitem.updated event.

    For workitem.updated events, resource.fields contains only the CHANGED
    fields, each with "oldValue" and "newValue".
    """

    def __init__(self, fields: dict[str, Any]) -> None:
        self._fields: dict[str, AdoWebhookFieldChange] = {}
        for key, val in fields.items():
            if isinstance(val, dict) and ("oldValue" in val or "newValue" in val):
                self._fields[key] = AdoWebhookFieldChange.model_validate(val)

    def get_change(self, field_ref: str) -> AdoWebhookFieldChange | None:
        """Get the change record for a specific field, or None if not changed."""
        return self._fields.get(field_ref)

    def has_changed(self, field_ref: str) -> bool:
        """Check whether a specific field was part of this update."""
        return field_ref in self._fields

    def new_value_for(self, field_ref: str) -> Any:
        """Get the new value for a changed field, or None if not changed."""
        change = self._fields.get(field_ref)
        return change.new_value if change else None

    def old_value_for(self, field_ref: str) -> Any:
        """Get the old value for a changed field, or None if not changed."""
        change = self._fields.get(field_ref)
        return change.old_value if change else None


class AdoWebhookRevision(BaseModel):
    """The 'revision' object inside a workitem.updated resource — the full current state."""

    id: int
    rev: int
    fields: dict[str, Any] = Field(default_factory=dict)

    def get_field(self, field_ref: str, default: Any = None) -> Any:
        """Get a field value from the revision's fields dict."""
        return self.fields.get(field_ref, default)


class AdoWebhookResource(BaseModel):
    """
    The 'resource' object in an ADO Service Hook payload.

    Handles both event types:
    - workitem.created: resource.fields is a flat dict {fieldRef: value},
      no revision object, no workItemId (just id)
    - workitem.updated: resource.fields has {fieldRef: {oldValue, newValue}},
      resource.revision has the full current state, has workItemId
    """

    id: int
    work_item_id: int | None = Field(default=None, alias="workItemId")
    rev: int
    revised_by: dict[str, Any] | None = Field(default=None, alias="revisedBy")
    revised_date: datetime | None = Field(default=None, alias="revisedDate")
    fields: dict[str, Any] = Field(default_factory=dict)
    revision: AdoWebhookRevision | None = None

    model_config = {"populate_by_name": True}

    def get_resource_id(self) -> int:
        """Get the work item ID — uses workItemId if present, falls back to id."""
        return self.work_item_id if self.work_item_id is not None else self.id

    def get_changed_fields(self) -> AdoWebhookChangedFields:
        """
        Wrap the raw fields dict in the change-tracking helper.
        Only meaningful for workitem.updated events.
        """
        return AdoWebhookChangedFields(self.fields)

    def get_work_item_type(self) -> str:
        """
        Get the work item type from the best available source.

        - workitem.created: type is in resource.fields directly
        - workitem.updated: type is in resource.revision.fields
        """
        if self.revision is not None:
            return self.revision.get_field(AdoFieldNames.WORK_ITEM_TYPE, "")
        return self.fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")

    def get_field(self, field_ref: str, default: Any = None) -> Any:
        """
        Get a field value from the best available source.

        - workitem.created: reads from resource.fields (flat dict)
        - workitem.updated: reads from resource.revision.fields (full state)
        """
        if self.revision is not None:
            return self.revision.get_field(field_ref, default)
        return self.fields.get(field_ref, default)

    @property
    def is_created_event(self) -> bool:
        """True if this resource came from a workitem.created event (no revision)."""
        return self.revision is None


class AdoWebhookPayload(BaseModel):
    """
    Top-level ADO Service Hook webhook payload.

    Handles both workitem.created and workitem.updated events.
    Reference: https://learn.microsoft.com/en-us/azure/devops/service-hooks/events
    """

    subscription_id: str = Field(alias="subscriptionId")
    notification_id: int = Field(alias="notificationId")
    event_type: str = Field(alias="eventType")
    resource: AdoWebhookResource
    resource_version: str | None = Field(default=None, alias="resourceVersion")
    resource_containers: dict[str, Any] | None = Field(default=None, alias="resourceContainers")

    model_config = {"populate_by_name": True}

    @property
    def is_created(self) -> bool:
        return self.event_type == "workitem.created"

    @property
    def is_updated(self) -> bool:
        return self.event_type == "workitem.updated"

    @property
    def project_id(self) -> str:
        """Extract the ADO project GUID from resourceContainers.project.id."""
        if self.resource_containers:
            project = self.resource_containers.get("project", {})
            return project.get("id", "") if isinstance(project, dict) else ""
        return ""
