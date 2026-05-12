"""
ADO REST API client wrapper.

Handles all communication with the Azure DevOps REST API for work items.
Uses httpx for HTTP requests and tenacity for retry/backoff.

Auth: Acquires an Azure AD bearer token for the ADO resource
(499b84ac-1321-427f-aa17-267ca6975798) via DefaultAzureCredential
(Managed Identity in Azure, Azure CLI locally). Tokens are cached and
auto-refreshed on expiry (~1 hour).

Reference: https://learn.microsoft.com/en-us/rest/api/azure/devops/wit/work-items
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.integration.config import Settings, settings as _global_settings
from src.integration.errors import AdoApiError, AdoWorkItemNotFoundError
from src.integration.models.ado_models import AdoFieldNames, AdoWorkItemTypes
from src.integration.services.content_parser import filter_content_for_client
from src.integration.utils.retry import retry_ado_api

logger = logging.getLogger(__name__)

# ADO API version for work item operations
ADO_API_VERSION = "7.1"

# Azure AD resource ID for Azure DevOps — used to request bearer tokens
ADO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"

# Refresh token 5 minutes before actual expiry to avoid mid-request failures
TOKEN_REFRESH_BUFFER_SECONDS = 300

# Re-export from shared module for backward compat within this file
from src.integration.utils.html_utils import html_content_equal as _html_content_equal


# ---------------------------------------------------------------------------
# Per-parent-type field sync configuration.
#
# Which fields are synced from parent → child depends on the parent work item
# type. Content fields are filtered through the [TAG] content parser; metadata
# fields are copied as-is.
# ---------------------------------------------------------------------------

_SYNC_FIELDS_BY_PARENT_TYPE: dict[str, tuple[list[str], list[str]]] = {
    AdoWorkItemTypes.USER_STORY: (
        # Content fields (tag-filtered)
        [
            AdoFieldNames.DESCRIPTION,
            AdoFieldNames.ACCEPTANCE_CRITERIA,
            AdoFieldNames.TEST_SCENARIOS,
            AdoFieldNames.RELEASE_NOTES,
        ],
        # Metadata fields (copied as-is)
        [
            AdoFieldNames.REQUEST_CATEGORY,
            AdoFieldNames.PRIORITY,
            AdoFieldNames.SCRUM_TEAM,
        ],
    ),
    AdoWorkItemTypes.BUG: (
        [
            AdoFieldNames.DESCRIPTION,
            AdoFieldNames.REPRO_STEPS,
            AdoFieldNames.TEST_SCENARIOS,
            AdoFieldNames.RELEASE_NOTES,
        ],
        [
            AdoFieldNames.REQUEST_CATEGORY,
            AdoFieldNames.PRIORITY,
            AdoFieldNames.SCRUM_TEAM,
            AdoFieldNames.SEVERITY,
        ],
    ),
    AdoWorkItemTypes.FEATURE: (
        [
            AdoFieldNames.DESCRIPTION,
            AdoFieldNames.ACCEPTANCE_CRITERIA,
        ],
        [
            AdoFieldNames.REQUEST_CATEGORY,
            AdoFieldNames.SCRUM_TEAM,
            AdoFieldNames.PRIORITY,
        ],
    ),
}

# Default for types not explicitly listed (Epic, Initiative) — matches User Story.
_DEFAULT_SYNC_FIELDS: tuple[list[str], list[str]] = _SYNC_FIELDS_BY_PARENT_TYPE[AdoWorkItemTypes.USER_STORY]


def _get_sync_fields_for_type(parent_type: str) -> tuple[list[str], list[str]]:
    """Return (content_fields, metadata_fields) for a given parent work item type."""
    return _SYNC_FIELDS_BY_PARENT_TYPE.get(parent_type, _DEFAULT_SYNC_FIELDS)


class AdoService:
    """Client for the Azure DevOps REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.ado_base_url

        # Token cache for Managed Identity auth
        self._credential = None
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

        self._client = httpx.Client(
            headers={
                "Content-Type": "application/json-patch+json",
            },
            timeout=30.0,
            event_hooks={"request": [self._inject_auth_header]},
        )

    def _inject_auth_header(self, request: httpx.Request) -> None:
        """httpx event hook — injects the Managed Identity bearer token on every request."""
        token = self._get_bearer_token()
        request.headers["Authorization"] = f"Bearer {token}"

    def _get_bearer_token(self) -> str:
        """
        Get a valid Azure AD bearer token for ADO, refreshing if expired.

        Uses DefaultAzureCredential which automatically picks up:
        - Managed Identity when running in Azure
        - Azure CLI credentials when running locally
        """
        now = time.time()
        if self._cached_token and now < self._token_expires_at:
            return self._cached_token

        if self._credential is None:
            from azure.identity import DefaultAzureCredential
            self._credential = DefaultAzureCredential()

        token = self._credential.get_token(f"{ADO_RESOURCE_ID}/.default")
        self._cached_token = token.token
        self._token_expires_at = token.expires_on - TOKEN_REFRESH_BUFFER_SECONDS

        logger.info(
            "Acquired ADO bearer token via Managed Identity",
            extra={
                "expires_in_seconds": int(token.expires_on - now),
                "action": "token_refresh",
            },
        )
        return self._cached_token

    @retry_ado_api
    def get_work_item(self, work_item_id: int, *, expand: str = "all") -> dict[str, Any]:
        """
        Fetch a single work item by ID with all fields.

        Args:
            work_item_id: The ADO work item ID.
            expand: Expand option — "all" includes fields, relations, links.

        Returns:
            Full work item JSON response.

        Raises:
            AdoWorkItemNotFoundError: If the work item does not exist.
            AdoApiError: On other API errors.
        """
        url = f"{self._base_url}/wit/workitems/{work_item_id}"
        params = {"api-version": ADO_API_VERSION, "$expand": expand}

        response = self._client.get(url, params=params)

        if response.status_code == 404:
            # ADO returns 404 for both "not found" and "no permission" — log
            # the response body so callers can distinguish the two cases.
            logger.warning(
                "ADO returned 404 for work item — may indicate missing permissions",
                extra={
                    "ado_id": work_item_id,
                    "response_body": response.text[:500],
                    "auth_mode": "managed_identity",
                    "action": "get_work_item",
                },
            )
            raise AdoWorkItemNotFoundError(f"Work item {work_item_id} not found")

        if not response.is_success:
            logger.error(
                "ADO API error fetching work item",
                extra={
                    "ado_id": work_item_id,
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "action": "get_work_item",
                },
            )

        response.raise_for_status()

        logger.info(
            "Fetched ADO work item",
            extra={"ado_id": work_item_id, "action": "get_work_item"},
        )
        return response.json()

    @retry_ado_api
    def get_work_item_updates(self, work_item_id: int, *, top: int = 2) -> list[dict[str, Any]]:
        """
        Fetch the latest revision updates for a work item.

        Uses the Work Item Updates API which returns per-revision diffs showing
        which fields changed (oldValue → newValue) in each revision.

        Args:
            work_item_id: The ADO work item ID.
            top: Number of most recent updates to fetch (default: 2 for current + previous).

        Returns:
            List of update dicts, newest first (reversed from API order).
        """
        url = f"{self._base_url}/wit/workitems/{work_item_id}/updates"
        # Get the total count first so we can fetch only the last N
        params: dict[str, Any] = {"api-version": ADO_API_VERSION, "$top": 1}
        response = self._client.get(url, params=params)
        response.raise_for_status()
        total = response.json().get("count", 0)

        if total == 0:
            return []

        # Fetch the last `top` updates by skipping earlier ones
        skip = max(0, total - top)
        params = {"api-version": ADO_API_VERSION, "$top": top, "$skip": skip}
        response = self._client.get(url, params=params)
        response.raise_for_status()

        updates = response.json().get("value", [])
        updates.reverse()  # newest first
        return updates

    def content_fields_changed(self, work_item_id: int) -> bool:
        """
        Check whether any description/content fields changed in the most recent
        revision of a work item.

        Fetches the latest update record from ADO and inspects it for changes to
        Description, Acceptance Criteria, Test Scenarios, or UI/UX AC.

        Returns True if any content field changed, False otherwise.
        Falls back to True on errors (safer to update than to skip).
        """
        content_fields = {
            AdoFieldNames.DESCRIPTION,
            AdoFieldNames.ACCEPTANCE_CRITERIA,
            AdoFieldNames.REPRO_STEPS,
            AdoFieldNames.TEST_SCENARIOS,
            AdoFieldNames.RELEASE_NOTES,
            AdoFieldNames.UI_UX_ACCEPTANCE_CRITERIA,
        }
        try:
            updates = self.get_work_item_updates(work_item_id, top=1)
            if not updates:
                return True  # No history available — assume changed

            latest = updates[0]
            changed = latest.get("fields", {})
            for field_ref in content_fields:
                if field_ref in changed:
                    change = changed[field_ref]
                    if isinstance(change, dict):
                        old = change.get("oldValue") or ""
                        new = change.get("newValue") or ""
                        if old != new:
                            logger.info(
                                "Content field changed in latest revision",
                                extra={"ado_id": work_item_id, "field": field_ref},
                            )
                            return True
            return False
        except Exception as e:
            logger.warning(
                "Could not check content field changes — assuming changed",
                extra={"ado_id": work_item_id, "error": str(e)},
            )
            return True  # Fail open — safer to update

    @retry_ado_api
    def create_work_item(
        self,
        work_item_type: str,
        fields: dict[str, Any],
        *,
        parent_id: int | None = None,
        project: str = "",
    ) -> dict[str, Any]:
        """
        Create a new work item in ADO.

        Args:
            work_item_type: The work item type (e.g., "Client Story Work Item").
            fields: Dict of field reference name → value to set.
            parent_id: Optional parent work item ID to create a parent-child link.
            project: ADO project name. Required — work item creation is project-scoped.

        Returns:
            The created work item JSON response (includes the new ID).

        Raises:
            AdoApiError: On API errors.
        """
        project = project or self._settings.ado_project
        project_api = self._settings.ado_project_url(project)
        url = f"{project_api}/wit/workitems/${work_item_type}"
        params = {"api-version": ADO_API_VERSION}

        # ADO uses JSON Patch format for work item creation
        operations: list[dict[str, Any]] = []

        for field_ref, value in fields.items():
            operations.append({
                "op": "add",
                "path": f"/fields/{field_ref}",
                "value": value,
            })

        # Add parent link if specified
        if parent_id is not None:
            org = self._settings.ado_organization
            parent_url = f"https://dev.azure.com/{org}/{project}/_apis/wit/workitems/{parent_id}"
            operations.append({
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": parent_url,
                    "attributes": {"comment": "Auto-created by ADO-HappyFox integration"},
                },
            })

        response = self._client.post(url, params=params, json=operations)

        # Surface the ADO response body on 4xx so we can see which field ADO
        # rejected (e.g., "Bug doesn't have field X"). Raise the non-retryable
        # AdoApiError directly so tenacity doesn't waste 5 attempts on a fatal
        # client error.
        if 400 <= response.status_code < 500:
            logger.error(
                "ADO rejected work item creation",
                extra={
                    "status_code": response.status_code,
                    "work_item_type": work_item_type,
                    "response_body": response.text[:1500],
                    "field_refs": list(fields.keys()),
                    "action": "create_work_item",
                },
            )
            raise AdoApiError(
                f"ADO rejected create_work_item for {work_item_type} "
                f"({response.status_code}): {response.text[:500]}"
            )
        response.raise_for_status()

        result = response.json()
        new_id = result.get("id")

        logger.info(
            "Created ADO work item",
            extra={
                "ado_id": new_id,
                "work_item_type": work_item_type,
                "parent_id": parent_id,
                "action": "create_work_item",
            },
        )
        return result

    @retry_ado_api
    def update_work_item(self, work_item_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        """
        Update specific fields on an existing work item.

        Args:
            work_item_id: The ADO work item ID to update.
            fields: Dict of field reference name → new value.

        Returns:
            The updated work item JSON response.

        Raises:
            AdoWorkItemNotFoundError: If the work item does not exist.
            AdoApiError: On other API errors.
        """
        url = f"{self._base_url}/wit/workitems/{work_item_id}"
        params = {"api-version": ADO_API_VERSION}

        operations = [
            {"op": "add", "path": f"/fields/{field_ref}", "value": value}
            for field_ref, value in fields.items()
        ]

        response = self._client.patch(url, params=params, json=operations)

        if response.status_code == 404:
            raise AdoWorkItemNotFoundError(f"Work item {work_item_id} not found")

        response.raise_for_status()

        logger.info(
            "Updated ADO work item",
            extra={
                "ado_id": work_item_id,
                "fields_updated": list(fields.keys()),
                "action": "update_work_item",
            },
        )
        return response.json()

    @retry_ado_api
    def get_comments(self, work_item_id: int) -> list[dict[str, Any]]:
        """
        Fetch all comments on a work item via the ADO Comments API.

        Returns comments ordered by creation date (oldest first).
        Each comment dict has: id, text (HTML), createdBy, createdDate, modifiedDate.

        Args:
            work_item_id: The ADO work item ID.

        Returns:
            List of comment dicts from the API.
        """
        url = f"{self._base_url}/wit/workItems/{work_item_id}/comments"
        params = {"api-version": f"{ADO_API_VERSION}-preview.4", "$top": 200}

        response = self._client.get(url, params=params)

        if response.status_code == 404:
            logger.warning(
                "ADO returned 404 for work item comments",
                extra={"ado_id": work_item_id, "action": "get_comments"},
            )
            return []

        response.raise_for_status()
        data = response.json()
        comments = data.get("comments", [])

        logger.info(
            "Retrieved ADO comments",
            extra={"ado_id": work_item_id, "comment_count": len(comments), "action": "get_comments"},
        )
        return comments

    def get_attachments(self, work_item_id: int) -> list[dict[str, Any]]:
        """
        Get attachment metadata for a work item.

        Attachments are stored in the work item's relations array where
        rel == "AttachedFile".

        Args:
            work_item_id: The ADO work item ID.

        Returns:
            List of attachment metadata dicts with 'url', 'name', 'attributes'.
        """
        work_item = self.get_work_item(work_item_id, expand="relations")
        relations = work_item.get("relations", []) or []

        attachments = [
            {
                "url": rel["url"],
                "name": rel.get("attributes", {}).get("name", "unknown"),
                "resource_size": rel.get("attributes", {}).get("resourceSize", 0),
            }
            for rel in relations
            if rel.get("rel") == "AttachedFile"
        ]

        logger.info(
            "Retrieved attachments",
            extra={"ado_id": work_item_id, "attachment_count": len(attachments)},
        )
        return attachments

    @retry_ado_api
    def download_attachment(self, attachment_url: str) -> bytes:
        """
        Download an attachment's content by URL.

        Args:
            attachment_url: The full URL to the attachment resource.

        Returns:
            Raw attachment bytes.
        """
        response = self._client.get(attachment_url)
        response.raise_for_status()
        return response.content

    @retry_ado_api
    def upload_attachment(
        self, file_bytes: bytes, filename: str, project: str = ""
    ) -> str:
        """
        Upload a file to the ADO attachment store and return its URL.

        The returned URL can then be linked to a work item via add_attachment_link().

        Args:
            file_bytes: Raw file content.
            filename: The filename to assign in ADO.
            project: ADO project (used for the API URL scope).

        Returns:
            The attachment URL from ADO (used to link to work items).
        """
        base = self._settings.ado_project_url(project) if project else self._base_url
        url = f"{base}/wit/attachments"
        params = {"api-version": ADO_API_VERSION, "fileName": filename}

        # Override Content-Type for binary upload
        response = self._client.post(
            url,
            params=params,
            content=file_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        response.raise_for_status()
        data = response.json()
        attachment_url = data.get("url", "")

        logger.info(
            "Uploaded attachment to ADO store",
            extra={"filename": filename, "size_bytes": len(file_bytes)},
        )
        return attachment_url

    @retry_ado_api
    def add_attachment_link(
        self, work_item_id: int, attachment_url: str, filename: str
    ) -> None:
        """
        Link an uploaded attachment to a work item.

        Args:
            work_item_id: Target work item ID.
            attachment_url: URL returned by upload_attachment().
            filename: Display name for the attachment.
        """
        url = f"{self._base_url}/wit/workitems/{work_item_id}"
        params = {"api-version": ADO_API_VERSION}

        operations = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "AttachedFile",
                    "url": attachment_url,
                    "attributes": {"name": filename},
                },
            }
        ]

        response = self._client.patch(url, params=params, json=operations)
        response.raise_for_status()

        logger.info(
            "Linked attachment to work item",
            extra={
                "ado_id": work_item_id,
                "filename": filename,
                "action": "add_attachment_link",
            },
        )

    def link_attachments_to_work_item(
        self, source_work_item_id: int, target_work_item_id: int
    ) -> int:
        """
        Link all attachments from one work item to another (no re-upload).

        ADO attachments live in a shared store — the same attachment URL can be
        linked to multiple work items. This just adds AttachedFile relations on
        the target pointing to the source's existing attachment URLs.

        Best-effort: logs and skips individual failures.

        Args:
            source_work_item_id: Work item to read attachments FROM.
            target_work_item_id: Work item to link attachments TO.

        Returns:
            Number of attachments successfully linked.
        """
        attachments = self.get_attachments(source_work_item_id)
        if not attachments:
            return 0

        linked = 0
        for att in attachments:
            filename = att.get("name", "unknown")
            att_url = att.get("url", "")
            try:
                self.add_attachment_link(target_work_item_id, att_url, filename)
                linked += 1
            except Exception as e:
                logger.warning(
                    "Failed to link attachment — skipping",
                    extra={
                        "source_id": source_work_item_id,
                        "target_id": target_work_item_id,
                        "filename": filename,
                        "error": str(e)[:300],
                    },
                )

        logger.info(
            "Linked attachments to work item",
            extra={
                "source_id": source_work_item_id,
                "target_id": target_work_item_id,
                "total": len(attachments),
                "linked": linked,
                "action": "link_attachments",
            },
        )
        return linked

    def get_child_work_items(self, parent_id: int) -> list[dict[str, Any]]:
        """
        Fetch all child work items linked to a parent via ADO relations.

        Fetches the parent with relations expanded, filters for
        System.LinkTypes.Hierarchy-Forward (parent→child), then fetches
        each child work item.

        Args:
            parent_id: The parent work item ID.

        Returns:
            List of child work item dicts (each includes 'id' and 'fields').
        """
        parent = self.get_work_item(parent_id, expand="relations")
        relations = parent.get("relations", []) or []

        # Hierarchy-Forward = parent → child link direction
        child_urls = [
            rel["url"]
            for rel in relations
            if rel.get("rel") == "System.LinkTypes.Hierarchy-Forward"
        ]

        if not child_urls:
            return []

        children: list[dict[str, Any]] = []
        for url in child_urls:
            # Extract work item ID from the URL
            try:
                child_id = int(url.rstrip("/").split("/")[-1])
                child = self.get_work_item(child_id)
                children.append(child)
            except (ValueError, Exception) as e:
                logger.warning(
                    "Failed to fetch child work item from relation URL",
                    extra={"url": url, "parent_id": parent_id, "error": str(e)},
                )

        logger.info(
            "Fetched child work items",
            extra={"parent_id": parent_id, "child_count": len(children)},
        )
        return children

    def get_parent_id(self, work_item_id: int) -> int | None:
        """
        Get the parent work item ID from a child's hierarchy relations.

        Fetches the work item with relations expanded and looks for a
        System.LinkTypes.Hierarchy-Reverse link (child → parent).

        Args:
            work_item_id: The child work item ID.

        Returns:
            The parent work item ID, or None if no parent link exists.
        """
        work_item = self.get_work_item(work_item_id, expand="relations")
        relations = work_item.get("relations", []) or []

        for rel in relations:
            if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
                try:
                    parent_id = int(rel["url"].rstrip("/").split("/")[-1])
                    logger.info(
                        "Resolved parent ID from hierarchy relation",
                        extra={"child_id": work_item_id, "parent_id": parent_id},
                    )
                    return parent_id
                except (ValueError, KeyError, IndexError):
                    pass

        return None

    @retry_ado_api
    def add_parent_link(
        self, child_id: int, parent_id: int, *, project: str = ""
    ) -> dict[str, Any]:
        """
        Add a Hierarchy-Reverse relation (child → parent) to an existing work item.

        Used when we create a parent after the child already exists (e.g., a
        HappyFox-originated Client work item that needs a mirrored parent).

        Args:
            child_id: The existing child work item to patch.
            parent_id: The newly created parent work item ID.
            project: ADO project name for building the parent link URL.

        Returns:
            The updated work item JSON response.

        Raises:
            AdoApiError: On API errors.
        """
        project = project or self._settings.ado_project
        url = f"{self._base_url}/wit/workitems/{child_id}"
        params = {"api-version": ADO_API_VERSION}

        org = self._settings.ado_organization
        parent_url = f"https://dev.azure.com/{org}/{project}/_apis/wit/workitems/{parent_id}"

        operations = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": parent_url,
                    "attributes": {"comment": "Auto-linked by ADO-HappyFox integration"},
                },
            },
        ]

        response = self._client.patch(url, params=params, json=operations)
        response.raise_for_status()

        logger.info(
            "Added parent link to work item",
            extra={
                "child_id": child_id,
                "parent_id": parent_id,
                "action": "add_parent_link",
            },
        )
        return response.json()

    def create_child_work_item(
        self,
        parent_id: int,
        child_type: str,
        client_name: str,
        parent_fields: dict[str, Any],
        portal_client_name: str = "",
        project: str = "",
    ) -> dict[str, Any]:
        """
        Create a child work item of the specified type for a specific client.

        Composes the title as "{client_name} - {ParentTitle}" and copies shared
        fields from the parent work item. Which fields are synced depends on the
        parent work item type (User Story, Bug, Feature, etc.).

        Args:
            parent_id: The parent work item ID.
            child_type: The ADO work item type to create (e.g. "Client Story Work Item").
            client_name: The client name from the Client Selection for Portal Syncing field.
                This is the portal-canonical name (1:1 with HappyFox choice text).
            parent_fields: The parent work item's fields dict.
            portal_client_name: Alias for client_name (kept for backward compat). When
                provided, takes precedence for title prefix and HF mapping.
            project: ADO project name — passed through to create_work_item.

        Returns:
            The created child work item JSON response.
        """
        parent_title = parent_fields.get(AdoFieldNames.TITLE, "Untitled")
        title_prefix = portal_client_name or client_name
        child_title = f"{title_prefix} - {parent_title}"

        # Fields set directly on the child.
        # Both CLIENT_REQUESTED and CLIENT_SELECTION_PORTAL are set to the
        # portal-sourced client name so HF transform can use exact match.
        # NOTE: Parent link is established via System.LinkTypes.Hierarchy-Reverse relation,
        # not via a Custom.ADOParentID field (which doesn't exist on this work item type).
        effective_client = portal_client_name or client_name
        child_fields: dict[str, Any] = {
            AdoFieldNames.TITLE: child_title,
            AdoFieldNames.CLIENT_REQUESTED: effective_client,
            AdoFieldNames.CLIENT_SELECTION_PORTAL: effective_client,
        }

        # Determine which fields to sync based on parent type
        parent_type = parent_fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")
        content_fields, metadata_fields = _get_sync_fields_for_type(parent_type)

        # Filter content fields through the tag parser.
        alias_map = _global_settings.get_client_alias_map()
        filter_client = portal_client_name or client_name
        for field_ref in content_fields:
            value = parent_fields.get(field_ref)
            if value is not None:
                filtered = filter_content_for_client(value, filter_client, alias_map)
                if filtered:
                    child_fields[field_ref] = filtered

        # Metadata fields copied as-is (only if present on parent)
        for field_ref in metadata_fields:
            value = parent_fields.get(field_ref)
            if value is not None:
                child_fields[field_ref] = value

        return self.create_work_item(
            work_item_type=child_type,
            fields=child_fields,
            parent_id=parent_id,
            project=project,
        )

    # Keep backward-compatible alias
    create_client_story = create_child_work_item

    def sync_child_from_parent(
        self,
        child_id: int,
        client_name: str,
        parent_fields: dict[str, Any],
        portal_client_name: str = "",
        only_fields: set[str] | None = None,
        child_state: str | None = None,
    ) -> dict[str, Any]:
        """
        Update an existing child work item's fields from the parent.

        Used during "Sync Now" to push parent field changes down to children.
        Updates the same shared fields that are copied during create_child_work_item,
        plus the title (recomposed from client name + parent title).

        Args:
            child_id: The existing child work item ID.
            client_name: The client name (from Client Selection for Portal Syncing).
            parent_fields: The parent work item's current fields dict.
            portal_client_name: Alias for client_name (kept for backward compat).
            only_fields: If provided, only sync fields in this set. None = sync all.
                         This prevents writing unchanged fields to the child, which
                         would create misleading ADO revision entries.
            child_state: If provided, set System.State on the child to this value.
                         Derived from the parent state via the per-type state mapping.

        Returns:
            The updated work item JSON response.
        """
        parent_title = parent_fields.get(AdoFieldNames.TITLE, "Untitled")
        title_prefix = portal_client_name or client_name
        child_title = f"{title_prefix} - {parent_title}"

        update_fields: dict[str, Any] = {}

        # Always sync title if Title changed or if only_fields is None (full sync)
        if only_fields is None or AdoFieldNames.TITLE in only_fields:
            update_fields[AdoFieldNames.TITLE] = child_title

        # Ensure portal field is set on the child for HF mapping
        if portal_client_name and (
            only_fields is None or AdoFieldNames.CLIENT_SELECTION_PORTAL in only_fields
        ):
            update_fields[AdoFieldNames.CLIENT_SELECTION_PORTAL] = portal_client_name

        # Determine which fields to sync based on parent type
        parent_type = parent_fields.get(AdoFieldNames.WORK_ITEM_TYPE, "")
        content_sync_fields, metadata_sync_fields = _get_sync_fields_for_type(parent_type)

        # Filter content fields through the tag parser
        alias_map = _global_settings.get_client_alias_map()
        filter_client = portal_client_name or client_name
        for field_ref in content_sync_fields:
            if only_fields is not None and field_ref not in only_fields:
                continue
            value = parent_fields.get(field_ref)
            if value is not None:
                filtered = filter_content_for_client(value, filter_client, alias_map)
                if filtered:
                    update_fields[field_ref] = filtered

        for field_ref in metadata_sync_fields:
            # Skip fields not in the changed set (if filtering is active)
            if only_fields is not None and field_ref not in only_fields:
                continue
            value = parent_fields.get(field_ref)
            if value is not None:
                update_fields[field_ref] = value

        # State propagation — set the child's state from the per-type mapping.
        if child_state:
            update_fields[AdoFieldNames.STATE] = child_state

        # ── Diff against current child fields to avoid writing unchanged values.
        # Writing the same value still creates an ADO revision, which misleads
        # the child processor's "content_fields_changed" check into thinking
        # the description was edited when it wasn't.
        #
        # HTML content fields need normalised comparison because ADO may
        # alter whitespace, entity encoding, or attribute order on save —
        # a plain string compare would always see them as "changed".
        html_fields = {
            AdoFieldNames.DESCRIPTION,
            AdoFieldNames.ACCEPTANCE_CRITERIA,
            AdoFieldNames.REPRO_STEPS,
            AdoFieldNames.TEST_SCENARIOS,
            AdoFieldNames.RELEASE_NOTES,
        }
        if update_fields:
            try:
                child_item = self.get_work_item(child_id)
                child_fields = child_item.get("fields", {})
                unchanged = []
                for field_ref, new_val in list(update_fields.items()):
                    current_val = child_fields.get(field_ref)
                    # Normalise None / empty string so "" == None == missing
                    if not new_val and not current_val:
                        unchanged.append(field_ref)
                        del update_fields[field_ref]
                    elif field_ref in html_fields:
                        # HTML-aware comparison: strip tags, normalise whitespace
                        if _html_content_equal(new_val, current_val):
                            unchanged.append(field_ref)
                            del update_fields[field_ref]
                    elif str(new_val or "").strip() == str(current_val or "").strip():
                        unchanged.append(field_ref)
                        del update_fields[field_ref]
                if unchanged:
                    logger.info(
                        "Skipped unchanged fields in child sync",
                        extra={
                            "child_id": child_id,
                            "skipped_fields": unchanged,
                        },
                    )
            except Exception as e:
                logger.warning(
                    "Could not diff child fields — syncing all fields",
                    extra={"child_id": child_id, "error": str(e)},
                )

        if not update_fields:
            logger.info(
                "No fields to sync to child — skipping ADO update",
                extra={"child_id": child_id, "client": client_name},
            )
            return {"_actually_written_fields": []}

        actually_written = list(update_fields.keys())

        logger.info(
            "Syncing child work item fields from parent",
            extra={
                "child_id": child_id,
                "client": client_name,
                "fields_synced": actually_written,
                "filtered": only_fields is not None,
            },
        )

        result = self.update_work_item(child_id, update_fields)
        result["_actually_written_fields"] = actually_written
        return result

    # Keep backward-compatible alias
    sync_client_story_from_parent = sync_child_from_parent

    def reset_sync_field(self, work_item_id: int, *, label: str = "work item") -> None:
        """Reset SyncToClientPortal to idle after a Sync Now event.

        Logs success/failure internally so callers don't need try/except blocks.

        Args:
            work_item_id: The ADO work item ID (parent or child).
            label: Human-readable label for log messages (e.g., "parent", "child").
        """
        reset_value = self._settings.ado_sync_to_client_portal_reset_value
        try:
            self.update_work_item(
                work_item_id,
                {AdoFieldNames.SYNC_TO_CLIENT_PORTAL: reset_value},
            )
            logger.info(
                "Reset SyncToClientPortal after Sync Now",
                extra={
                    "ado_id": work_item_id,
                    "label": label,
                    "reset_value": reset_value,
                    "action": "sync_now_reset",
                },
            )
        except Exception as e:
            logger.warning(
                "Failed to reset SyncToClientPortal after Sync Now",
                extra={
                    "ado_id": work_item_id,
                    "label": label,
                    "reset_value": reset_value,
                    "error": str(e),
                    "action": "sync_now_reset",
                },
            )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
