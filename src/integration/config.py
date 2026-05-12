"""
Centralized configuration via pydantic-settings.

All secrets and settings are loaded from environment variables (or .env file for local dev).
Never scatter os.getenv() calls in function handlers — use this module instead.

MAPPINGS:
    All ADO ↔ HappyFox mappings are stored as JSON strings in Azure App Settings.
    This lets the team update client lists, project mappings, etc. in the Azure Portal
    without redeploying the code. Each mapping has a hardcoded default that matches the
    current production values, so nothing breaks if the App Setting isn't configured yet.

    To update a mapping: Azure Portal → Function App → Settings → Environment variables
    → edit the JSON value → save → the app restarts automatically.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default mapping values — used as fallbacks if env vars are not set.
# These are the production values as of 2026-04-16.
# ---------------------------------------------------------------------------

_DEFAULT_CLIENT_TO_HF: dict[str, int] = {
    "Alabama Parks": 62,
    "Arkansas Parks": 130,
    "Avaratak": 167,
    "Bahamas H/F": 128,
    "Colorado H/F and Parks": 114,
    "Florida H/F": 132,
    "Georgia H/F and Parks": 154,
    "Georgia Power": 143,
    "Idaho H/F": 145,
    "Idaho Parks": 140,
    "Indiana H/F": 136,
    "Iowa H/F": 152,
    "Kansas H/F": 151,
    "Lake Casitas Parks": 138,
    "Louisiana Parks": 129,
    "Maryland H/F": 146,
    "Massachusetts H/F": 149,
    "Muskingum Parks": 135,
    "Nebraska H/F": 137,
    "North Carolina H/F": 126,
    "Oklahoma H/F": 148,
    "Oregon H/F": 142,
    "South Carolina H/F": 141,
    "South Carolina Parks": 153,
    "South Dakota H/F and Parks": 127,
    "Tennessee H/F": 133,
    "Tennessee Parks": 150,
    "USVI H/F": 131,
    "Virginia H/F": 147,
    "Washington H/F": 144,
    "West Virginia H/F": 134,
}

# Keyed by ADO project GUID (from resourceContainers.project.id in webhook payload).
# Must be configured via MAPPING_PROJECT_TO_PRODUCT app setting with your actual
# project GUIDs. Example: {"a1b2c3d4-...": 68, "e5f6g7h8-...": 69}
_DEFAULT_PROJECT_TO_PRODUCT: dict[str, int] = {}

_DEFAULT_REQUEST_CATEGORY_TO_HF: dict[str, int] = {
    "Configuration": 20,
    "Cosmetic Improvement": 193,
    "Customization / Improvement": 248,
    "Data Change/Update": 17,
    "Data Migration / Conversion": 198,
    "Data Request / Query": 23,
    "Database / Schema Change": 199,
    "Deployment / Network / Infrastructure": 196,
    "Documentation": 22,
    "Edit Report": 7,
    "Implementation": 119,
    "Internal Admin": 194,
    "Legislative/Rule Change": 11,
    "Marketing": 15,
    "New Report": 16,
    "New System Feature": 24,
    "Performance Improvement / Optimization": 190,
    "Production Bug": 18,
    "Security Improvement": 192,
    "Standing Service Order": 6,
    "Technical Debt": 191,
    "Test Defect / Bug": 13,
    "Workflow / Feature Improvement": 195,
}

_DEFAULT_PRIORITY_TO_HF: dict[str, int] = {
    "1": 2,  # Urgent
    "2": 3,  # High
    "3": 1,  # Moderate
    "4": 4,  # Low
}

_DEFAULT_SCRUM_TEAM_TO_HF: dict[str, int] = {
    "GO Customers and Vehicles": 37,
    "GO Licensing": 28,
    "GO Mobile": 26,
    "GO Payments": 36,
    "GO Platform": 31,
    "GO Reservations": 33,
    "Itinio": 222,
    "Terra East": 35,
    "Terra West": 30,
}

_DEFAULT_UAT_STATUS_TO_HF: dict[str, int] = {
    "Failed UAT": 160,
    "In UAT Environment": 197,
    "Not in UAT": 161,                         # HF choice text
    "Not in UAT Environment": 161,             # ADO picklist value
    "Passed UAT / Ready for Production": 159,
}

_DEFAULT_REQUIREMENTS_ACCEPTANCE_TO_HF: dict[str, int] = {
    # Keys are ADO picklist values; values are HappyFox choice IDs.
    # NOTE: ADO and HF have slightly different text for two values:
    #   ADO: "Modification" (singular) vs HF: "Modifications" (plural)
    #   ADO: "Not Submitted" vs HF: "Not Yet Submitted"
    # Both ADO and HF variants are included so mapping works regardless of source.
    "Modification to Requirements Requested": 158,   # ADO picklist value
    "Modifications to Requirements Requested": 158,  # HF choice text
    "Requirements Accepted": 155,
    "Requirements Not Submitted": 156,               # ADO picklist value
    "Requirements Not Yet Submitted": 156,           # HF choice text
    "Requirements Pending Acceptance": 157,
}

# Client alias mapping — maps short codes AND full client names to full client names.
# Short codes let the product team use [GOF] instead of full names in descriptions.
# Full-name entries (e.g., "Alabama Parks" → "Alabama Parks") guarantee that
# [Client Name] tags always resolve correctly for content parsing.
_DEFAULT_CLIENT_ALIAS: dict[str, str] = {
    # --- Short-code aliases ---
    "ADCNR": "Alabama Parks",
    "ADPHT": "Arkansas Parks",
    "CPW": "Colorado H/F and Parks",
    "GOF": "Florida H/F",
    "GADNR": "Georgia H/F and Parks",
    "GAPOWER": "Georgia Power",
    "BAHAMAS": "Bahamas H/F",
    "IDFG": "Idaho H/F",
    "IDPR": "Idaho Parks",
    "IDNR": "Indiana H/F",
    "IOWA": "Iowa H/F",
    "KDWP": "Kansas H/F",
    "LASP": "Louisiana Parks",
    "MDDNR": "Maryland H/F",
    "MassDFG": "Massachusetts H/F",
    "MWCD": "Muskingum Parks",
    "NGPC": "Nebraska H/F",
    "NCWRC": "North Carolina H/F",
    "ODWC": "Oklahoma H/F",
    "ODFW": "Oregon H/F",
    "SCDNR": "South Carolina H/F",
    "SCPRT": "South Carolina Parks",
    "SDGFP": "South Dakota H/F and Parks",
    "TSP": "Tennessee Parks",
    "TWRA": "Tennessee H/F",
    "VIDPNR": "USVI H/F",
    "GOV": "Virginia H/F",
    "WDFW": "Washington H/F",
    "WVDNR": "West Virginia H/F",
    # --- Full-name aliases (ensures exact client name works as [TAG]) ---
    "Alabama Parks": "Alabama Parks",
    "Arkansas Parks": "Arkansas Parks",
    "Avaratak": "Avaratak",
    "Bahamas H/F": "Bahamas H/F",
    "Colorado H/F and Parks": "Colorado H/F and Parks",
    "Florida H/F": "Florida H/F",
    "Georgia H/F and Parks": "Georgia H/F and Parks",
    "Georgia Power": "Georgia Power",
    "Idaho H/F": "Idaho H/F",
    "Idaho Parks": "Idaho Parks",
    "Indiana H/F": "Indiana H/F",
    "Iowa H/F": "Iowa H/F",
    "Kansas H/F": "Kansas H/F",
    "Lake Casitas Parks": "Lake Casitas Parks",
    "Louisiana Parks": "Louisiana Parks",
    "Maryland H/F": "Maryland H/F",
    "Massachusetts H/F": "Massachusetts H/F",
    "Muskingum Parks": "Muskingum Parks",
    "Nebraska H/F": "Nebraska H/F",
    "North Carolina H/F": "North Carolina H/F",
    "Oklahoma H/F": "Oklahoma H/F",
    "Oregon H/F": "Oregon H/F",
    "South Carolina H/F": "South Carolina H/F",
    "South Carolina Parks": "South Carolina Parks",
    "South Dakota H/F and Parks": "South Dakota H/F and Parks",
    "Tennessee H/F": "Tennessee H/F",
    "Tennessee Parks": "Tennessee Parks",
    "USVI H/F": "USVI H/F",
    "Virginia H/F": "Virginia H/F",
    "Washington H/F": "Washington H/F",
    "West Virginia H/F": "West Virginia H/F",
}

# NOTE: HappyFox does not currently have a "Feature" choice for Issue Type - Development.
# If a "Feature" choice is added in HF, add its ID here.
_DEFAULT_ISSUE_TYPE_DEV_TO_HF: dict[str, int] = {
    "Bug": 122,
    "Epic": 123,
    "Initiative": 125,
    "Story": 124,
}


# ---------------------------------------------------------------------------
# State / status mappings — derived from the "ADO to HF Status Mapping"
# workbook. Keyed by ADO *parent* work item type.
# ---------------------------------------------------------------------------

# Parent ADO state → child ADO state. Only types with non-1:1 mappings are
# listed. User Story and Bug have identical parent/child states.
_DEFAULT_STATE_TO_CHILD_STATE: dict[str, dict[str, str]] = {
    "Epic": {
        "New": "New",
        "Backlog": "New",
        "Open": "In Progress",
        "Resolved": "Completed",
        "In Progress": "In Progress",
        "Completed": "Completed",
        "Removed": "Removed",
    },
    "Initiative": {
        "New": "New",
        "Backlog": "Backlog",
        "In Progress": "In Progress",
        "Completed": "Completed",
        "Removed": "Removed",
    },
    "Feature": {
        "New": "New",
        "Backlog": "Backlog",
        "In Progress": "In Progress",
        "Completed": "Completed",
        "Removed": "Removed",
    },
}

# Parent ADO state → HappyFox ticket status ID (numeric).
# User Story and Bug share the same mapping.
# Status IDs: 1=New, 3=On Hold, 4=Closed, 5=DEV - Sent to Dev,
#   7=DEV - Ready for Development, 8=DEV - Under Analysis,
#   13=DEV - Selected for Development, 14=DEV - In QA/Testing,
#   15=DEV - Ready for Release
_DEFAULT_STATE_TO_HF_STATUS: dict[str, dict[str, int]] = {
    "User Story": {
        "New": 5,                        # DEV - Sent to Dev
        "Ready for Refinement": 8,       # DEV - Under Analysis
        "Ready for Development": 7,      # DEV - Ready for Development
        "Backlog": 3,                    # On Hold
        "Intake - Requirements": 8,      # DEV - Under Analysis
        "Intake - Product Review": 8,    # DEV - Under Analysis
        "Intake - Tech Scope": 8,        # DEV - Under Analysis
        "Resolved": 4,                   # Closed
        "In Progress": 13,               # DEV - Selected for Development
        "Selected for Development": 13,  # DEV - Selected for Development
        "Pull Request": 13,              # DEV - Selected for Development
        "Ready for QA": 14,              # DEV - In QA/Testing
        "Passes QA Testing": 14,         # DEV - In QA/Testing
        "Completed": 4,                  # Closed
    },
    "Bug": {
        "New": 5,                        # DEV - Sent to Dev
        "Ready for Refinement": 8,       # DEV - Under Analysis
        "Ready for Development": 7,      # DEV - Ready for Development
        "Backlog": 3,                    # On Hold
        "Intake - Requirements": 8,      # DEV - Under Analysis
        "Intake - Product Review": 8,    # DEV - Under Analysis
        "Intake - Tech Scope": 8,        # DEV - Under Analysis
        "Resolved": 4,                   # Closed
        "In Progress": 13,               # DEV - Selected for Development
        "Selected for Development": 13,  # DEV - Selected for Development
        "Pull Request": 13,              # DEV - Selected for Development
        "Ready for QA": 14,              # DEV - In QA/Testing
        "Passes QA Testing": 14,         # DEV - In QA/Testing
        "Completed": 4,                  # Closed
    },
    "Epic": {
        "New": 5,                        # DEV - Sent to Dev
        "Backlog": 3,                    # On Hold
        "Open": 4,                       # Closed
        "Resolved": 4,                   # Closed
        "In Progress": 13,               # DEV - Selected for Development
        "Ready for Testing": 14,         # DEV - In QA/Testing
        "Completed": 4,                  # Closed
        "Removed": 4,                    # Closed
    },
    "Initiative": {
        "New": 5,                        # DEV - Sent to Dev
        "Backlog": 3,                    # On Hold
        "Resolved": 4,                   # Closed
        "In Progress": 13,               # DEV - Selected for Development
        "Completed": 4,                  # Closed
        "Removed": 4,                    # Closed
    },
    "Feature": {
        "New": 5,                        # DEV - Sent to Dev
        "Backlog": 3,                    # On Hold
        "In Progress": 13,               # DEV - Selected for Development
        "Completed": 4,                  # Closed
        "Removed": 4,                    # Closed
    },
}


def _parse_json_mapping(raw: str, name: str) -> dict[str, Any]:
    """Parse a JSON string into a dict, logging errors gracefully."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Failed to parse %s mapping from env var: %s", name, e)
        return {}


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Azure Service Bus ---
    service_bus_connection_string: SecretStr
    ado_parent_events_queue: str = "ado-parent-events"
    ado_child_events_queue: str = "ado-child-events"

    # --- Azure DevOps ---
    ado_organization: str
    ado_project: str = ""  # DEPRECATED — kept for backward compat; prefer per-event project from payload
    ado_webhook_secret: SecretStr | None = None  # HMAC shared secret — optional, not currently used

    # --- ADO Custom Field References ---
    # These are the "Custom.*" field reference names from your ADO process template.
    # Editable in Azure App Settings if your org renames fields.
    # Standard "System.*" and "Microsoft.*" fields are NOT externalized — they never change.
    ado_field_client_requested: str = "Custom.ClientRequested"
    ado_field_client_selection_portal: str = "Custom.ClientSelectionforPortalVisibility"
    ado_field_request_category: str = "Custom.RequestCategory"
    ado_field_test_scenarios: str = "Custom.TestScenarios"
    ado_field_uat_feedback_resolved: str = "Custom.UATFeedbackResolved"
    ado_field_ui_ux_acceptance_criteria: str = "Custom.UIandUXAcceptanceCriteria"
    ado_field_is_new_functionality: str = "Custom.IsNewFunctionality"
    ado_field_uat_deployment_status: str = "Custom.UATDeploymentStatus"
    ado_field_sync_to_client_portal: str = "Custom.SyncToClientPortal"
    ado_field_production_release_date: str = "Custom.ProductionReleaseDate"
    ado_field_release_version: str = "Custom.ReleaseVersion"
    ado_field_integration_sprint: str = "Custom.IntegrationSprint"
    ado_field_scrum_team: str = "Custom.ScrumTeam"
    ado_field_uat_env_deployment_status: str = "Custom.UATEnvironmentDeploymentStatus"
    ado_field_support_ticket_number: str = "Custom.SupportTicketNumber"
    ado_field_support_ticket_status: str = "Custom.SupportTicketStatus"
    ado_field_support_ticket_title: str = "Custom.SupportTicketTitle"
    ado_field_ado_parent_id: str = "Custom.ADOParentID"
    ado_field_requirements_approval_status: str = "Custom.RequirementsApprovalStatus"
    ado_field_release_approval: str = "Custom.ReleaseApproval"
    ado_field_release_notes: str = "Custom.ReleaseNotes"
    ado_field_contract_requirement_numbers: str = "Custom.ContractRequirementNumbers"
    ado_field_latest_sync_date: str = "Custom.LatestSyncDate"

    # --- ADO Trigger Values ---
    # The exact string values that trigger integration events.
    # Editable in Azure App Settings if ADO picklist options are renamed.
    ado_trigger_state_ready: str = "Ready for Development"
    ado_trigger_sync_now: str = "Sync Now"
    ado_trigger_uat_in_environment: str = "In UAT Environment"

    # Value to write back into SyncToClientPortal on the parent after a
    # "Sync Now" event has been processed, so the picklist resets to an
    # idle state and the user can trigger another sync.
    ado_sync_to_client_portal_reset_value: str = "None"

    # --- HappyFox ---
    happyfox_api_url: str = "https://yoursubdomain.happyfox.com/api/1.1/json"
    happyfox_api_key: SecretStr
    happyfox_auth_code: SecretStr
    happyfox_webhook_secret: SecretStr | None = None

    # --- HappyFox Defaults ---
    hf_default_category_id: int = 2                                           # Development category
    hf_default_product_id: int = 69                                           # Itinio (fallback)
    hf_default_priority_id: int = 1                                            # Moderate (fallback)
    hf_default_priority_name: str = "Moderate"                                # Fallback priority (kept for backward compat)
    hf_default_status_id: int = 1                                              # Default open status (1=New, resolved directly by ID)
    hf_create_user_name: str = "Avaratak"
    hf_create_user_email: str = "lori.tragesser+test@brandtinfo.com"
    hf_update_user_name: str = "Kyle Bring"
    hf_update_user_email: str = "kyle.bring@brandtinfo.com"
    hf_staff_match_patterns: str = "kyle.bring,avaratak,lori.tragesser+test"  # Comma-separated (fallback for staff ID lookup)

    # --- HappyFox Custom Field IDs ---
    # Format: "t-cf-{id}" — editable if HF fields are reconfigured.
    hf_cf_request_category: str = "t-cf-2"
    hf_cf_scrum_team: str = "t-cf-3"
    hf_cf_client_requested: str = "t-cf-5"
    hf_cf_product: str = "t-cf-8"
    hf_cf_dev_ticket_number: str = "t-cf-29"
    hf_cf_release_version: str = "t-cf-39"
    hf_cf_uat_status: str = "t-cf-41"
    hf_cf_parent: str = "t-cf-42"
    hf_cf_dev_parent_number: str = "t-cf-45"

    # New fields for multi-work-item-type support (confirmed from HF API 2026-04-08).
    hf_cf_issue_type_dev: str = "t-cf-38"          # "Issue Type - Development" (choice)
    hf_cf_requirements_acceptance: str = "t-cf-40" # "Requirement Acceptance Status" (choice)
    hf_cf_contract_requirements: str = "t-cf-37"   # "Contract Requirement #s" (text)
    hf_cf_ado_project: str = "t-cf-62"             # "ADO Project" (text)
    hf_cf_ado_work_item_type: str = "t-cf-63"      # "ADO Work Item Type" (text)
    hf_cf_ado_work_item_title: str = "t-cf-64"     # "ADO Work Item Title" (text)
    hf_cf_ado_ticket_state: str = "t-cf-65"        # "ADO Ticket State" (text)

    # --- Mappings (JSON strings — editable in Azure App Settings) ---
    # Each is a JSON object: {"key": value, ...}
    # See docs/app-settings.md for the full reference.
    mapping_client_to_hf: str = ""             # Client name → HF choice ID
    mapping_project_to_product: str = ""       # ADO project ID (GUID) → HF Product choice ID
    mapping_request_category: str = ""         # Request category → HF choice ID
    mapping_priority: str = ""                 # ADO priority (1-4) → HF priority ID
    mapping_scrum_team: str = ""               # Scrum Team text → HF choice ID
    mapping_uat_status: str = ""               # UAT Status text → HF choice ID
    mapping_requirements_acceptance: str = ""  # Requirements Acceptance text → HF choice ID
    mapping_issue_type_dev: str = ""           # Issue Type value (Story/Bug/etc.) → HF choice ID
    mapping_client_alias: str = ""             # Short alias → full client name (for [TAG] parsing)
    mapping_state_to_child_state: str = ""   # Parent ADO state → child ADO state (per parent type)
    mapping_state_to_hf_status: str = ""     # Parent ADO state → HF status ID (per parent type)

    # --- Runtime ---
    log_level: str = "INFO"
    environment: str = "development"  # development | staging | production
    child_dedup_window_seconds: int = 90  # Ignore duplicate child events within this window

    # --- Computed properties ---
    @property
    def ado_base_url(self) -> str:
        """ADO REST API base URL (org-level — works for all projects)."""
        return f"https://dev.azure.com/{self.ado_organization}/_apis"

    def ado_project_url(self, project: str) -> str:
        """ADO REST API base URL scoped to a specific project."""
        return f"https://dev.azure.com/{self.ado_organization}/{project}/_apis"

    # --- Mapping accessors ---
    # These parse the JSON env vars on first access and fall back to defaults.
    # Simple flat mappings delegate to _get_mapping(); nested and special-case
    # accessors are defined individually below.

    @staticmethod
    def _get_mapping(
        raw: str,
        env_name: str,
        default: dict,
        *,
        cast_key: type = str,
        cast_val: type = int,
    ) -> dict:
        """Generic JSON mapping loader with type casting and fallback.

        Args:
            raw: The raw JSON string from the env var (may be empty).
            env_name: Env var name for error logging.
            default: Default dict to copy when *raw* is empty or unparseable.
            cast_key: Type to cast keys to (``str`` or ``int``).
            cast_val: Type to cast values to (``int`` or ``str``).
        """
        if raw:
            parsed = _parse_json_mapping(raw, env_name)
            if parsed:
                return {cast_key(k): cast_val(v) for k, v in parsed.items()}
        # Return a shallow copy so callers can't mutate the module-level default.
        return {cast_key(k): cast_val(v) for k, v in default.items()}

    def get_client_to_hf_map(self) -> dict[str, int]:
        """ADO client text → HappyFox 'Client Requested' choice ID."""
        return self._get_mapping(self.mapping_client_to_hf, "MAPPING_CLIENT_TO_HF", _DEFAULT_CLIENT_TO_HF)

    def get_project_to_product_map(self) -> dict[str, int]:
        """ADO project ID (GUID) → HappyFox Product choice ID."""
        return self._get_mapping(self.mapping_project_to_product, "MAPPING_PROJECT_TO_PRODUCT", _DEFAULT_PROJECT_TO_PRODUCT)

    def get_request_category_map(self) -> dict[str, int]:
        """ADO request category → HappyFox Request Category choice ID."""
        return self._get_mapping(self.mapping_request_category, "MAPPING_REQUEST_CATEGORY", _DEFAULT_REQUEST_CATEGORY_TO_HF)

    def get_priority_map(self) -> dict[int, int]:
        """ADO priority int (1-4) → HappyFox priority ID."""
        return self._get_mapping(self.mapping_priority, "MAPPING_PRIORITY", _DEFAULT_PRIORITY_TO_HF, cast_key=int)

    def get_scrum_team_map(self) -> dict[str, int]:
        """ADO Scrum Team text → HappyFox Scrum Team choice ID."""
        return self._get_mapping(self.mapping_scrum_team, "MAPPING_SCRUM_TEAM", _DEFAULT_SCRUM_TEAM_TO_HF)

    def get_uat_status_map(self) -> dict[str, int]:
        """ADO UAT Status text → HappyFox UAT Status choice ID."""
        return self._get_mapping(self.mapping_uat_status, "MAPPING_UAT_STATUS", _DEFAULT_UAT_STATUS_TO_HF)

    def get_requirements_acceptance_map(self) -> dict[str, int]:
        """ADO Requirements Acceptance Status text → HappyFox choice ID."""
        return self._get_mapping(self.mapping_requirements_acceptance, "MAPPING_REQUIREMENTS_ACCEPTANCE", _DEFAULT_REQUIREMENTS_ACCEPTANCE_TO_HF)

    def get_issue_type_dev_map(self) -> dict[str, int]:
        """HF Issue Type value (Story, Bug, Epic, etc.) → HappyFox choice ID."""
        return self._get_mapping(self.mapping_issue_type_dev, "MAPPING_ISSUE_TYPE_DEV", _DEFAULT_ISSUE_TYPE_DEV_TO_HF)

    def get_client_alias_map(self) -> dict[str, str]:
        """Short alias (e.g., 'GOF') → full client name."""
        return self._get_mapping(self.mapping_client_alias, "MAPPING_CLIENT_ALIAS", _DEFAULT_CLIENT_ALIAS, cast_val=str)

    def _get_nested_str_mapping(self, raw: str, env_name: str, default: dict, parent_type: str) -> dict[str, str]:
        """Load a nested JSON mapping keyed by parent work item type (str values)."""
        if raw:
            parsed = _parse_json_mapping(raw, env_name)
            if parsed:
                return {k: str(v) for k, v in parsed.get(parent_type, {}).items()}
        return dict(default.get(parent_type, {}))

    def _get_nested_int_mapping(self, raw: str, env_name: str, default: dict, parent_type: str) -> dict[str, int]:
        """Load a nested JSON mapping keyed by parent work item type (int values)."""
        if raw:
            parsed = _parse_json_mapping(raw, env_name)
            if parsed:
                return {k: int(v) for k, v in parsed.get(parent_type, {}).items()}
        return {k: int(v) for k, v in default.get(parent_type, {}).items()}

    def get_state_to_child_state_map(self, parent_type: str) -> dict[str, str]:
        """Parent ADO state → child ADO state for a given parent work item type.

        Returns an empty dict if the parent/child states are 1:1 (User Story, Bug),
        meaning the parent state should be copied as-is to the child.
        """
        return self._get_nested_str_mapping(
            self.mapping_state_to_child_state, "MAPPING_STATE_TO_CHILD_STATE",
            _DEFAULT_STATE_TO_CHILD_STATE, parent_type,
        )

    def get_state_to_hf_status_map(self, parent_type: str) -> dict[str, int]:
        """Parent ADO state → HappyFox status ID (numeric) for a given parent work item type.

        IDs are used directly — no runtime name→ID lookup needed.
        """
        return self._get_nested_int_mapping(
            self.mapping_state_to_hf_status, "MAPPING_STATE_TO_HF_STATUS",
            _DEFAULT_STATE_TO_HF_STATUS, parent_type,
        )

    def get_staff_match_patterns(self) -> list[str]:
        """List of lowercase patterns to match the HF staff account."""
        return [p.strip().lower() for p in self.hf_staff_match_patterns.split(",") if p.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Module-level singleton — import this in function handlers
settings = Settings()
