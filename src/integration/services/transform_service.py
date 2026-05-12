"""
Field mapping: ADO Client Story ↔ HappyFox ticket schema translation.

This module is the code implementation of docs/field-mapping.md.
If you change one, update the other — they must stay in sync.

IMPORTANT: HappyFox status, priority, and category IDs are numeric and
environment-specific. They must be resolved at runtime via the HappyFox API,
never hardcoded.

All mapping dictionaries and custom field IDs are loaded from centralized
settings (src.integration.config). Defaults live there too, editable as
Azure App Settings without redeploying code.
"""

from __future__ import annotations

import logging
from typing import Any

from src.integration.config import settings
from src.integration.models.ado_models import AdoFieldNames, AdoWorkItemTypes
from src.integration.models.happyfox_models import HappyFoxTicketCreate, HappyFoxTicketUpdate
from src.integration.services.happyfox_service import HappyFoxService

logger = logging.getLogger(__name__)


def _resolve_request_category_choice_id(category_name: str) -> int | None:
    """
    Resolve an ADO Request Category to a HappyFox choice ID.

    Tries:
    1. Exact match
    2. Case-insensitive exact match
    3. Case-insensitive substring match
    """
    category_map = settings.get_request_category_map()

    # Exact match first
    if category_name in category_map:
        return category_map[category_name]

    # Case-insensitive matching
    cat_lower = category_name.strip().lower()
    for hf_text, choice_id in category_map.items():
        if hf_text.lower() == cat_lower:
            logger.info(
                "Case-insensitive matched ADO request category to HF choice",
                extra={"ado_category": category_name, "hf_choice_text": hf_text, "choice_id": choice_id},
            )
            return choice_id

    # Substring fallback
    for hf_text, choice_id in category_map.items():
        hf_lower = hf_text.lower()
        if cat_lower in hf_lower or hf_lower in cat_lower:
            logger.info(
                "Fuzzy-matched ADO request category to HF choice",
                extra={"ado_category": category_name, "hf_choice_text": hf_text, "choice_id": choice_id},
            )
            return choice_id

    return None


def _resolve_client_choice_id(client_name: str) -> int | None:
    """
    Resolve an ADO client name to a HappyFox "Client Requested" choice ID.

    ADO stores short names (e.g., "Alabama Parks") while HappyFox stores full names
    (e.g., "Alabama State Parks (ADCNR)"). This function tries:
    1. Exact match
    2. Case-insensitive prefix match (ADO name starts HF choice text)
    3. Case-insensitive substring match (ADO name appears in HF choice text)
    """
    client_map = settings.get_client_to_hf_map()

    # Exact match first
    if client_name in client_map:
        return client_map[client_name]

    # Case-insensitive prefix / substring match
    client_lower = client_name.strip().lower()
    for hf_text, choice_id in client_map.items():
        hf_lower = hf_text.lower()
        if hf_lower.startswith(client_lower) or client_lower in hf_lower:
            logger.info(
                "Fuzzy-matched ADO client to HF choice",
                extra={"ado_client": client_name, "hf_choice_text": hf_text, "choice_id": choice_id},
            )
            return choice_id

    return None


def _resolve_uat_status_choice_id(status_text: str) -> int | None:
    """
    Resolve an ADO UAT status value to a HappyFox UAT Status choice ID.

    Both UAT Deployment Status ("Failed UAT", "Passed UAT / Ready for Production")
    and UAT Environment Deployment Status ("In UAT Environment", "Not in UAT Environment")
    map to the same HappyFox field (t-cf-41).

    Tries:
    1. Exact match
    2. Case-insensitive exact match
    3. Case-insensitive substring match
    """
    uat_map = settings.get_uat_status_map()

    # Exact match first
    if status_text in uat_map:
        return uat_map[status_text]

    # Case-insensitive matching
    status_lower = status_text.strip().lower()
    for hf_text, choice_id in uat_map.items():
        if hf_text.lower() == status_lower:
            logger.info(
                "Case-insensitive matched ADO UAT status to HF choice",
                extra={"ado_value": status_text, "hf_choice_text": hf_text, "choice_id": choice_id},
            )
            return choice_id

    # Substring fallback
    for hf_text, choice_id in uat_map.items():
        hf_lower = hf_text.lower()
        if status_lower in hf_lower or hf_lower in status_lower:
            logger.info(
                "Fuzzy-matched ADO UAT status to HF choice",
                extra={"ado_value": status_text, "hf_choice_text": hf_text, "choice_id": choice_id},
            )
            return choice_id

    return None


def _resolve_requirements_acceptance_choice_id(status_text: str) -> int | None:
    """
    Resolve an ADO Requirements Acceptance Status to a HappyFox choice ID.

    Tries:
    1. Exact match
    2. Case-insensitive exact match
    3. Case-insensitive substring match
    """
    req_map = settings.get_requirements_acceptance_map()

    # Exact match first
    if status_text in req_map:
        return req_map[status_text]

    # Case-insensitive matching
    status_lower = status_text.strip().lower()
    for hf_text, choice_id in req_map.items():
        if hf_text.lower() == status_lower:
            logger.info(
                "Case-insensitive matched ADO Requirements Acceptance to HF choice",
                extra={"ado_value": status_text, "hf_choice_text": hf_text, "choice_id": choice_id},
            )
            return choice_id

    # Substring fallback
    for hf_text, choice_id in req_map.items():
        hf_lower = hf_text.lower()
        if status_lower in hf_lower or hf_lower in status_lower:
            logger.info(
                "Fuzzy-matched ADO Requirements Acceptance to HF choice",
                extra={"ado_value": status_text, "hf_choice_text": hf_text, "choice_id": choice_id},
            )
            return choice_id

    return None


def _compose_description(fields: dict[str, Any]) -> str:
    """
    Compose the HappyFox ticket description body from multiple ADO fields.

    The three core content fields — Description, Acceptance Criteria, and
    Test Scenarios — are ALWAYS included with their headings, even when
    empty. This ensures HF tickets maintain a consistent structure and
    every update reflects the full picture rather than just the changed
    field.

    Bug types additionally include Repro Steps (replaces Acceptance Criteria).
    Release Notes and UI/UX AC are included only when populated.
    """
    sections: list[str] = []

    # ── Core fields: always present ──────────────────────────────────
    description = fields.get(AdoFieldNames.DESCRIPTION) or ""
    sections.append(f"<h3>Description</h3>\n{description}")

    acceptance = fields.get(AdoFieldNames.ACCEPTANCE_CRITERIA) or ""
    sections.append(f"<h3>Acceptance Criteria</h3>\n{acceptance}")

    test_scenarios = fields.get(AdoFieldNames.TEST_SCENARIOS) or ""
    sections.append(f"<h3>Test Scenarios</h3>\n{test_scenarios}")

    # ── Conditional fields: only when populated ──────────────────────
    # Repro Steps — Bug type only (replaces Acceptance Criteria)
    repro_steps = fields.get(AdoFieldNames.REPRO_STEPS) or ""
    if repro_steps:
        sections.append(f"<h3>Repro Steps</h3>\n{repro_steps}")

    release_notes = fields.get(AdoFieldNames.RELEASE_NOTES) or ""
    if release_notes:
        sections.append(f"<h3>Release Notes</h3>\n{release_notes}")

    # UI/UX AC is only present on parent User Story type — may be absent on child
    ui_ux = fields.get(AdoFieldNames.UI_UX_ACCEPTANCE_CRITERIA) or ""
    if ui_ux:
        sections.append(f"<h3>UI and UX Acceptance Criteria</h3>\n{ui_ux}")

    return "\n\n".join(sections)


def _build_custom_fields(
    fields: dict[str, Any],
    *,
    child_work_item_type: str = "",
    project_id: str = "",
    ado_child_id: str | None = None,
    ado_parent_title: str | None = None,
    is_create: bool = False,
    changed_fields: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build the HappyFox custom_fields dict from ADO child work item fields.

    Shared by both create and update transforms.  Behavioral differences:

    * **Product**: On create, always set (falls back to default). On update,
      only set when the project mapping resolves successfully.
    * **DEV Ticket Number / Parent**: Only set on create.
    * **Null handling**: On create, empty-string fields are skipped.
      On update, ``is not None`` checks allow explicit empty strings through
      (HappyFox interprets them as "clear the field").
    * **UAT Status**: Two ADO fields feed one HF field. ``changed_fields``
      determines which one takes priority ("last update wins").
    """
    custom_fields: dict[str, Any] = {}
    log_id = ado_child_id or ""

    # ── Product ──────────────────────────────────────────────────────
    project_map = settings.get_project_to_product_map()
    lookup_key = project_id or fields.get(AdoFieldNames.TEAM_PROJECT, "")
    if is_create:
        product_choice_id = project_map.get(lookup_key, settings.hf_default_product_id)
        if lookup_key and lookup_key not in project_map:
            logger.warning(
                "ADO project not found in HF product mapping — using default",
                extra={
                    "project_id": project_id,
                    "project_name": fields.get(AdoFieldNames.TEAM_PROJECT, ""),
                    "default_product_id": settings.hf_default_product_id,
                },
            )
        custom_fields[settings.hf_cf_product] = product_choice_id
    elif lookup_key:
        product_choice_id = project_map.get(lookup_key)
        if product_choice_id is not None:
            custom_fields[settings.hf_cf_product] = product_choice_id

    # ── Client Requested ─────────────────────────────────────────────
    client_map = settings.get_client_to_hf_map()
    portal_client = fields.get(AdoFieldNames.CLIENT_SELECTION_PORTAL, "")
    client = fields.get(AdoFieldNames.CLIENT_REQUESTED, "")
    if portal_client:
        client_choice_id = client_map.get(portal_client)
        if client_choice_id is None:
            client_choice_id = _resolve_client_choice_id(portal_client)
        if client_choice_id is not None:
            custom_fields[settings.hf_cf_client_requested] = client_choice_id
        else:
            logger.warning(
                "ADO portal client not found in HF choice mapping",
                extra={"portal_client": portal_client, "ado_child_id": log_id},
            )
    elif client:
        client_choice_id = _resolve_client_choice_id(client)
        if client_choice_id is not None:
            custom_fields[settings.hf_cf_client_requested] = client_choice_id
        elif is_create:
            logger.warning(
                "ADO client name not found in HF choice mapping",
                extra={"client": client, "ado_child_id": log_id},
            )

    # ── Request Category ─────────────────────────────────────────────
    request_category = fields.get(AdoFieldNames.REQUEST_CATEGORY, "" if is_create else None)
    if request_category:
        cat_choice_id = _resolve_request_category_choice_id(request_category)
        if cat_choice_id is not None:
            custom_fields[settings.hf_cf_request_category] = cat_choice_id
        else:
            logger.warning(
                "ADO request category not found in HF choice mapping",
                extra={"request_category": request_category, "ado_child_id": log_id},
            )

    # ── Release Version (text) ───────────────────────────────────────
    release_version = fields.get(AdoFieldNames.RELEASE_VERSION, "" if is_create else None)
    if (is_create and release_version) or (not is_create and release_version is not None):
        custom_fields[settings.hf_cf_release_version] = release_version

    # ── Scrum Team (choice) ──────────────────────────────────────────
    scrum_team = fields.get(AdoFieldNames.SCRUM_TEAM, "" if is_create else None)
    if (is_create and scrum_team) or (not is_create and scrum_team is not None):
        scrum_team_map = settings.get_scrum_team_map()
        scrum_choice_id = scrum_team_map.get(scrum_team)
        if scrum_choice_id is not None:
            custom_fields[settings.hf_cf_scrum_team] = scrum_choice_id
        elif scrum_team:
            logger.warning(
                "ADO Scrum Team not found in HF choice mapping",
                extra={"scrum_team": scrum_team, "ado_child_id": log_id, "is_create": is_create},
            )

    # ── UAT Status (choice) ──────────────────────────────────────────
    # Two ADO fields feed one HF field (t-cf-41):
    #   - UAT Environment Deployment Status: "In UAT Environment", "Not in UAT Environment"
    #   - UAT Deployment Status: "Failed UAT", "Passed UAT / Ready for Production"
    # "Last update wins": use changed_fields to pick which field triggered this
    # event. On create or full sync (changed_fields=None), use whichever has a value.
    uat_env_status = fields.get(AdoFieldNames.UAT_ENV_DEPLOYMENT_STATUS, "" if is_create else None)
    uat_deploy_status = fields.get(AdoFieldNames.UAT_DEPLOYMENT_STATUS, "" if is_create else None)

    # Determine which field to use based on what just changed
    changed_set = set(changed_fields) if changed_fields else set()
    uat_deploy_changed = AdoFieldNames.UAT_DEPLOYMENT_STATUS in changed_set
    uat_env_changed = AdoFieldNames.UAT_ENV_DEPLOYMENT_STATUS in changed_set

    if uat_deploy_changed:
        # UAT Deployment Status was just updated — use it
        uat_status = uat_deploy_status
    elif uat_env_changed:
        # UAT Env Deployment Status was just updated — use it
        uat_status = uat_env_status
    else:
        # Full sync or neither changed: prefer whichever has a value
        uat_status = uat_deploy_status or uat_env_status

    if (is_create and uat_status) or (not is_create and uat_status is not None):
        uat_choice_id = _resolve_uat_status_choice_id(uat_status) if uat_status else None
        if uat_choice_id is not None:
            custom_fields[settings.hf_cf_uat_status] = uat_choice_id
        elif uat_status:
            logger.warning(
                "ADO UAT Status not found in HF choice mapping",
                extra={"uat_status": uat_status, "ado_child_id": log_id, "is_create": is_create,
                       "available_choices": list(settings.get_uat_status_map().keys())},
            )

    # ── Requirements Acceptance Status (choice) ──────────────────────
    req_acceptance = fields.get(AdoFieldNames.REQUIREMENT_APPROVAL, "" if is_create else None)
    if (is_create and req_acceptance) or (not is_create and req_acceptance is not None):
        if settings.hf_cf_requirements_acceptance:
            req_choice_id = _resolve_requirements_acceptance_choice_id(req_acceptance) if req_acceptance else None
            if req_choice_id is not None:
                custom_fields[settings.hf_cf_requirements_acceptance] = req_choice_id
            elif req_acceptance:
                logger.warning(
                    "ADO Requirements Acceptance Status not found in HF choice mapping",
                    extra={
                        "req_acceptance": req_acceptance,
                        "ado_child_id": log_id,
                        "is_create": is_create,
                        "available_choices": list(settings.get_requirements_acceptance_map().keys()),
                    },
                )

    # ── Contract Requirement #s (text) ───────────────────────────────
    contract_req = fields.get(AdoFieldNames.CONTRACT_REQUIREMENT_NUMBERS, "" if is_create else None)
    if (is_create and contract_req) or (not is_create and contract_req is not None):
        if settings.hf_cf_contract_requirements:
            custom_fields[settings.hf_cf_contract_requirements] = contract_req

    # ── Issue Type - Development (choice) ────────────────────────────
    if child_work_item_type and settings.hf_cf_issue_type_dev:
        hf_issue_type = AdoWorkItemTypes.get_hf_issue_type(child_work_item_type)
        if hf_issue_type:
            issue_type_map = settings.get_issue_type_dev_map()
            issue_type_choice_id = issue_type_map.get(hf_issue_type)
            if issue_type_choice_id is not None:
                custom_fields[settings.hf_cf_issue_type_dev] = issue_type_choice_id
            elif is_create:
                logger.warning(
                    "HF Issue Type choice ID not found for value",
                    extra={"hf_issue_type": hf_issue_type, "child_type": child_work_item_type},
                )

    # ── ADO Project (text) ──────────────────────────────────────────
    ado_project = fields.get(AdoFieldNames.TEAM_PROJECT, "" if is_create else None)
    if (is_create and ado_project) or (not is_create and ado_project is not None):
        if settings.hf_cf_ado_project:
            custom_fields[settings.hf_cf_ado_project] = ado_project

    # ── ADO Work Item Type (text) ───────────────────────────────────
    ado_wi_type = fields.get(AdoFieldNames.WORK_ITEM_TYPE, "" if is_create else None)
    if (is_create and ado_wi_type) or (not is_create and ado_wi_type is not None):
        if settings.hf_cf_ado_work_item_type:
            custom_fields[settings.hf_cf_ado_work_item_type] = ado_wi_type

    # ── ADO Work Item Title (text) ──────────────────────────────────
    ado_title = fields.get(AdoFieldNames.TITLE, "" if is_create else None)
    if (is_create and ado_title) or (not is_create and ado_title is not None):
        if settings.hf_cf_ado_work_item_title:
            custom_fields[settings.hf_cf_ado_work_item_title] = ado_title

    # ── ADO Ticket State (text) ─────────────────────────────────────
    ado_state = fields.get(AdoFieldNames.STATE, "" if is_create else None)
    if (is_create and ado_state) or (not is_create and ado_state is not None):
        if settings.hf_cf_ado_ticket_state:
            custom_fields[settings.hf_cf_ado_ticket_state] = ado_state

    # ── Create-only fields ───────────────────────────────────────────
    if is_create:
        if ado_parent_title:
            custom_fields[settings.hf_cf_parent] = ado_parent_title
        if ado_child_id:
            custom_fields[settings.hf_cf_dev_ticket_number] = ado_child_id

    return custom_fields


def ado_client_story_to_happyfox_create(
    fields: dict[str, Any],
    *,
    hf_service: HappyFoxService,
    category_id: int,
    ado_parent_title: str = "",
    ado_child_id: str,
    child_work_item_type: str = "",
    project_id: str = "",
) -> HappyFoxTicketCreate:
    """
    Transform ADO child work item fields into a HappyFox ticket creation payload.

    Args:
        fields: The ADO child work item's fields dict (field ref name → value).
        hf_service: HappyFox service (retained for interface compatibility; no longer
            used for status resolution since statuses are mapped by ID directly).
        category_id: The HappyFox category ID to assign the ticket to.
        ado_parent_title: The ADO parent work item title for the HF "Parent" field.
        ado_child_id: The ADO child work item ID.
        child_work_item_type: The ADO child work item type (e.g., "Client Story Work Item").

    Returns:
        HappyFoxTicketCreate ready to submit to the API.
    """
    # Map ADO priority (1-4) directly to HF priority ID
    priority_map = settings.get_priority_map()
    ado_priority = fields.get(AdoFieldNames.PRIORITY, 3)
    hf_priority_id = priority_map.get(ado_priority, settings.hf_default_priority_id)

    # Default "open" status — resolved directly by ID, no API lookup needed.
    hf_status_id = settings.hf_default_status_id

    body = _compose_description(fields)

    custom_fields = _build_custom_fields(
        fields,
        child_work_item_type=child_work_item_type,
        project_id=project_id,
        ado_child_id=ado_child_id,
        ado_parent_title=ado_parent_title,
        is_create=True,
    )

    title = fields.get(AdoFieldNames.TITLE, "Untitled")

    return HappyFoxTicketCreate(
        subject=title,
        text=body,
        category=category_id,
        priority=hf_priority_id,
        status=hf_status_id,
        custom_fields=custom_fields,
    )


def ado_client_story_to_happyfox_update(
    fields: dict[str, Any],
    *,
    hf_service: HappyFoxService,
    child_work_item_type: str = "",
    hf_status_id: int | None = None,
    project_id: str = "",
    changed_fields: list[str] | None = None,
) -> HappyFoxTicketUpdate:
    """
    Transform ADO child work item fields into a HappyFox ticket update payload.

    Only includes fields that have values (partial update).

    Args:
        fields: The ADO child work item's fields dict (field ref name → value).
        hf_service: HappyFox service (retained for interface compatibility; no longer
            used for status resolution since statuses are mapped by ID directly).
        child_work_item_type: The ADO child work item type (e.g., "Client Story Work Item").
        hf_status_id: If provided, use directly as the HappyFox ticket status ID.
            Passed from the parent processor's state mapping (already numeric).
        changed_fields: List of ADO field reference names that changed in this webhook
            event. Used for "last update wins" logic (e.g., UAT fields).

    Returns:
        HappyFoxTicketUpdate ready to submit to the API.
    """
    update_fields: dict[str, Any] = {}

    # Map priority if present — ADO int (1-4) directly to HF priority ID
    priority_map = settings.get_priority_map()
    ado_priority = fields.get(AdoFieldNames.PRIORITY)
    if ado_priority is not None:
        hf_priority_id = priority_map.get(ado_priority)
        if hf_priority_id is not None:
            update_fields["priority"] = hf_priority_id

    # Recompose description if any content fields changed
    has_content_change = any(
        fields.get(f) is not None
        for f in [
            AdoFieldNames.DESCRIPTION,
            AdoFieldNames.ACCEPTANCE_CRITERIA,
            AdoFieldNames.TEST_SCENARIOS,
            AdoFieldNames.UI_UX_ACCEPTANCE_CRITERIA,
        ]
    )
    if has_content_change:
        update_fields["text"] = _compose_description(fields)

    # Special case: when Requirements Acceptance Status changes to
    # "Requirements Pending Acceptance", push the Acceptance Criteria to
    # the HF ticket message so the client can review it — even if the AC
    # field itself didn't change in this event.  This intentionally
    # OVERRIDES the full composite description (if one was set above)
    # with ONLY the AC so the customer sees exactly what they need to
    # accept.
    if (
        changed_fields
        and AdoFieldNames.REQUIREMENT_APPROVAL in changed_fields
        and fields.get(AdoFieldNames.REQUIREMENT_APPROVAL)
        == "Requirements Pending Acceptance"
    ):
        acceptance = fields.get(AdoFieldNames.ACCEPTANCE_CRITERIA) or ""
        update_fields["text"] = f"<h3>Acceptance Criteria</h3>\n{acceptance}"

    custom_fields = _build_custom_fields(
        fields,
        child_work_item_type=child_work_item_type,
        project_id=project_id,
        is_create=False,
        changed_fields=changed_fields,
    )

    title = fields.get(AdoFieldNames.TITLE)

    return HappyFoxTicketUpdate(
        subject=title,
        text=update_fields.get("text"),
        priority=update_fields.get("priority"),
        custom_fields=custom_fields,
        status=hf_status_id,
    )
