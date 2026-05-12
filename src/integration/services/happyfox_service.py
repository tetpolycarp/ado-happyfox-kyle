"""
HappyFox REST API client wrapper.

Handles all communication with the HappyFox Help Desk API.
Uses httpx for HTTP requests and tenacity for retry/backoff.

Reference: https://support.happyfox.com/kb/article/43-api-reference/
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.integration.config import Settings
from src.integration.errors import (
    HappyFoxApiError,
    HappyFoxRateLimitError,
    HappyFoxTicketNotFoundError,
)
from src.integration.models.happyfox_models import (
    HappyFoxCategoryResponse,
    HappyFoxPriorityResponse,
    HappyFoxStatusResponse,
    HappyFoxTicketCreate,
    HappyFoxTicketResponse,
    HappyFoxTicketUpdate,
)
from src.integration.utils.retry import retry_happyfox_api

logger = logging.getLogger(__name__)


class HappyFoxService:
    """Client for the HappyFox Help Desk REST API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = settings.happyfox_api_url.rstrip("/")
        self._client = httpx.Client(
            auth=(
                settings.happyfox_api_key.get_secret_value(),
                settings.happyfox_auth_code.get_secret_value(),
            ),
            timeout=30.0,
        )

        # Cached metadata — populated on first use
        self._statuses: list[HappyFoxStatusResponse] | None = None
        self._priorities: list[HappyFoxPriorityResponse] | None = None
        self._categories: list[HappyFoxCategoryResponse] | None = None
        self._default_staff_id: int | None = None
        self._staff_id_fetched: bool = False

        # Per-request ticket data cache — avoids redundant GET /ticket/{id}/
        # calls when multiple helpers (attachments, description, comments)
        # need the same ticket within a single processing cycle.
        self._ticket_data_cache: dict[int, dict] = {}

    def _check_rate_limit(self, response: httpx.Response) -> None:
        """Check rate limit headers and raise if exceeded."""
        if response.status_code == 429:
            remaining = response.headers.get("X-RateLimit-Remaining", "0")
            logger.warning(
                "HappyFox rate limit hit",
                extra={"remaining": remaining, "status_code": 429},
            )
            raise HappyFoxRateLimitError(
                f"HappyFox API rate limit exceeded. Remaining: {remaining}"
            )

    @retry_happyfox_api
    def get_ticket(self, ticket_id: int) -> HappyFoxTicketResponse | None:
        """
        Fetch a ticket by ID.

        Args:
            ticket_id: The HappyFox ticket ID.

        Returns:
            HappyFoxTicketResponse if found, None if 404.

        Raises:
            HappyFoxApiError: On other API errors.
        """
        url = f"{self._base_url}/ticket/{ticket_id}/"
        response = self._client.get(url)
        self._check_rate_limit(response)

        if response.status_code == 404:
            return None

        response.raise_for_status()

        logger.info(
            "Fetched HappyFox ticket",
            extra={"hf_ticket_id": ticket_id, "action": "get_ticket"},
        )
        return HappyFoxTicketResponse.model_validate(response.json())

    @retry_happyfox_api
    def create_ticket(self, payload: HappyFoxTicketCreate) -> HappyFoxTicketResponse:
        """
        Create a new ticket in HappyFox.

        Args:
            payload: The ticket creation payload.

        Returns:
            The created ticket response (includes the new ID).

        Raises:
            HappyFoxApiError: On API errors.
        """
        url = f"{self._base_url}/tickets/"
        api_payload = payload.to_api_payload()

        response = self._client.post(url, json=api_payload)
        self._check_rate_limit(response)

        if not response.is_success:
            logger.error(
                "Failed to create HappyFox ticket",
                extra={
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "action": "create_ticket",
                },
            )
            response.raise_for_status()

        result = HappyFoxTicketResponse.model_validate(response.json())

        logger.info(
            "Created HappyFox ticket",
            extra={"hf_ticket_id": result.id, "subject": result.subject, "action": "create_ticket"},
        )
        return result

    @retry_happyfox_api
    def update_ticket(self, ticket_id: int, payload: HappyFoxTicketUpdate) -> HappyFoxTicketResponse:
        """
        Update an existing ticket in HappyFox.

        Uses the staff_update endpoint. This endpoint requires:
        - 'staff' (int): Staff user ID performing the update.
        - Custom fields at top level as t-cf-* keys.
        - Optional: 'text' for adding a note, 'status', 'priority', etc.

        Args:
            ticket_id: The HappyFox ticket ID.
            payload: The ticket update payload.

        Returns:
            The updated ticket response.

        Raises:
            HappyFoxTicketNotFoundError: If the ticket doesn't exist.
            HappyFoxApiError: On other API errors.
        """
        url = f"{self._base_url}/ticket/{ticket_id}/staff_update/"
        api_payload = payload.to_api_payload()

        # staff_update requires a staff user ID
        staff_id = self._get_default_staff_id()
        if staff_id is not None:
            api_payload["staff"] = staff_id

        # If the caller included text (i.e. description changed), send it.
        # HTML content goes via "html" key so HappyFox renders tags properly.
        # When text is None (no description change), omit it entirely so we
        # don't post a redundant message — metadata-only updates (priority,
        # custom fields, subject) don't need a visible message body.
        text_content = api_payload.pop("text", None)
        if text_content and ("<" in text_content and ">" in text_content):
            api_payload["html"] = text_content
        elif text_content:
            api_payload["text"] = text_content

        logger.info(
            "Sending HappyFox ticket update",
            extra={
                "hf_ticket_id": ticket_id,
                "payload_keys": list(api_payload.keys()),
                "action": "update_ticket",
            },
        )

        response = self._client.post(url, json=api_payload)
        self._check_rate_limit(response)

        if response.status_code == 404:
            raise HappyFoxTicketNotFoundError(f"HappyFox ticket {ticket_id} not found")

        # HappyFox returns 400 "Nothing to update" when all sent values match
        # the existing ticket state. This is expected after Sync Now when fields
        # haven't actually changed — treat as a no-op, not a retryable error.
        if response.status_code == 400:
            body_text = response.text or ""
            if "nothing to update" in body_text.lower():
                logger.warning(
                    "HappyFox 400 — nothing to update (all values match existing ticket)",
                    extra={
                        "hf_ticket_id": ticket_id,
                        "response_body": body_text[:500],
                        "action": "update_ticket_noop",
                    },
                )
                # Return a minimal response — the ticket is already up to date.
                # Fetch the current ticket state so the caller gets a valid response.
                current = self.get_ticket(ticket_id)
                if current is not None:
                    return current
                # If we can't fetch the ticket either, fall through to error handling.

        if not response.is_success:
            logger.error(
                "Failed to update HappyFox ticket",
                extra={
                    "hf_ticket_id": ticket_id,
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "action": "update_ticket",
                },
            )
            response.raise_for_status()

        logger.info(
            "Updated HappyFox ticket",
            extra={"hf_ticket_id": ticket_id, "action": "update_ticket"},
        )
        return HappyFoxTicketResponse.model_validate(response.json())

    @retry_happyfox_api
    def add_attachment(self, ticket_id: int, filename: str, file_bytes: bytes) -> dict[str, Any]:
        """
        Upload an attachment to an existing ticket via staff_update.

        Uses multipart form data with the staff ID. The staff_update endpoint
        requires either text or an attachment, plus the staff field.

        Args:
            ticket_id: The HappyFox ticket ID.
            filename: The filename for the attachment.
            file_bytes: Raw file content bytes.

        Returns:
            API response dict.
        """
        url = f"{self._base_url}/ticket/{ticket_id}/staff_update/"

        # Build form data — staff_update needs staff + text/html + content
        staff_id = self._get_default_staff_id()
        data: dict[str, Any] = {
            "text": f"Attachment synced from Azure DevOps: {filename}",
        }
        if staff_id is not None:
            data["staff"] = str(staff_id)

        # HappyFox expects multipart form data for attachments
        files = [("attachments", (filename, file_bytes))]
        response = self._client.post(url, data=data, files=files)
        self._check_rate_limit(response)

        if not response.is_success:
            logger.error(
                "Failed to upload attachment to HappyFox",
                extra={
                    "hf_ticket_id": ticket_id,
                    "attachment_name": filename,
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                },
            )
            response.raise_for_status()

        logger.info(
            "Uploaded attachment to HappyFox ticket",
            extra={
                "hf_ticket_id": ticket_id,
                "attachment_name": filename,
                "size_bytes": len(file_bytes),
                "action": "add_attachment",
            },
        )
        return response.json()

    def upload_inline_images(
        self,
        ticket_id: int,
        images: list[tuple[str, bytes]],
    ) -> dict[str, str]:
        """
        Upload images as attachments and return a filename→URL mapping.

        Sends all images in a single ``staff_update`` call so only one
        message appears on the ticket timeline. The response includes the
        full ticket JSON; we parse the latest update's attachments to
        extract the HappyFox-hosted URLs.

        Args:
            ticket_id: The HappyFox ticket ID.
            images: List of (filename, raw_bytes) tuples.

        Returns:
            Dict mapping each filename to its HappyFox attachment URL.
            Files that fail to parse from the response are omitted.
        """
        if not images:
            return {}

        url = f"{self._base_url}/ticket/{ticket_id}/staff_update/"

        staff_id = self._get_default_staff_id()
        data: dict[str, Any] = {
            "text": "Inline images synced from Azure DevOps.",
        }
        if staff_id is not None:
            data["staff"] = str(staff_id)

        files = [("attachments", (name, content)) for name, content in images]
        response = self._client.post(url, data=data, files=files)
        self._check_rate_limit(response)

        if not response.is_success:
            logger.error(
                "Failed to upload inline images to HappyFox",
                extra={
                    "hf_ticket_id": ticket_id,
                    "image_count": len(images),
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                },
            )
            response.raise_for_status()

        # Parse attachment URLs from the response.
        result = response.json()
        url_map: dict[str, str] = {}

        # The latest update is the one we just created.
        updates = result.get("updates", [])
        if updates:
            latest = updates[-1]
            message = latest.get("message") or {}
            for att in message.get("attachments", []):
                fname = att.get("filename", "")
                att_url = att.get("url", "")
                if fname and att_url:
                    url_map[fname] = att_url

        logger.info(
            "Uploaded inline images to HappyFox",
            extra={
                "hf_ticket_id": ticket_id,
                "uploaded": len(images),
                "urls_resolved": len(url_map),
                "action": "upload_inline_images",
            },
        )

        # Invalidate cache since we just mutated the ticket.
        self.invalidate_ticket_cache(ticket_id)

        return url_map

    def _fetch_ticket_data(self, ticket_id: int) -> dict:
        """Fetch full ticket JSON, using a per-instance cache to avoid redundant calls.

        The cache lives for the lifetime of this HappyFoxService instance (one
        processing cycle). Callers that need fresh data after a mutation should
        call ``invalidate_ticket_cache(ticket_id)`` first.
        """
        if ticket_id in self._ticket_data_cache:
            return self._ticket_data_cache[ticket_id]

        url = f"{self._base_url}/ticket/{ticket_id}/"
        response = self._client.get(url)
        response.raise_for_status()
        data = response.json()
        self._ticket_data_cache[ticket_id] = data
        return data

    def invalidate_ticket_cache(self, ticket_id: int) -> None:
        """Remove a ticket from the per-instance cache after a mutation."""
        self._ticket_data_cache.pop(ticket_id, None)

    def get_ticket_attachment_names(self, ticket_id: int) -> set[str]:
        """
        Get the set of attachment filenames already on a HappyFox ticket.

        Used to avoid uploading duplicates when syncing attachments.
        """
        try:
            data = self._fetch_ticket_data(ticket_id)

            names: set[str] = set()
            for update in data.get("updates", []):
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                for attachment in message.get("attachments", []):
                    name = attachment.get("filename", "")
                    if name:
                        names.add(name)
            return names
        except Exception as e:
            logger.warning(
                "Could not fetch existing attachments for dedup",
                extra={"hf_ticket_id": ticket_id, "error": str(e)},
            )
            return set()

    def get_latest_description_html(self, ticket_id: int) -> str:
        """
        Get the most recently posted *description* content on a HappyFox ticket.

        Walks updates newest-first looking for the last staff reply that contains
        description section headers (e.g., <h3>Description</h3>). This distinguishes
        description updates from comment sync private notes and other messages.

        If no description update is found, falls back to the ticket's initial
        message (the first update, which is always the original creation body).

        Returns empty string if nothing is found or on error.
        """
        try:
            data = self._fetch_ticket_data(ticket_id)

            updates = data.get("updates", [])
            if not updates:
                return ""

            # Walk newest-first: find the most recent update whose content
            # looks like a composed description (contains our section headers).
            for update in reversed(updates):
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                html = message.get("html", "") or ""
                text = message.get("text", "") or ""
                content = html or text
                if content and "<h3>" in content:
                    return content

            # Fallback: return the very first update (ticket creation body).
            first_msg = updates[0].get("message")
            if isinstance(first_msg, dict):
                html = first_msg.get("html", "") or ""
                text = first_msg.get("text", "") or ""
                return html or text

            return ""
        except Exception as e:
            logger.warning(
                "Could not fetch ticket description for change detection",
                extra={"hf_ticket_id": ticket_id, "error": str(e)},
            )
            return ""

    @retry_happyfox_api
    def add_staff_note(self, ticket_id: int, html: str) -> dict[str, Any]:
        """
        Add a private note to a HappyFox ticket (visible to staff only).

        Uses the staff_pvtnote endpoint — the dedicated private note endpoint
        that creates yellow-highlighted internal notes not visible to contacts.
        Confirmed via endpoint testing: only staff_pvtnote returns message_type="p".

        Args:
            ticket_id: The HappyFox ticket ID.
            html: HTML content of the note.

        Returns:
            API response dict.
        """
        url = f"{self._base_url}/ticket/{ticket_id}/staff_pvtnote/"
        payload: dict[str, Any] = {"html": html}

        staff_id = self._get_default_staff_id()
        if staff_id is not None:
            payload["staff"] = staff_id

        response = self._client.post(url, json=payload)
        self._check_rate_limit(response)

        if not response.is_success:
            logger.error(
                "Failed to add private note to HappyFox ticket",
                extra={
                    "hf_ticket_id": ticket_id,
                    "status_code": response.status_code,
                    "response_body": response.text[:500],
                    "action": "add_staff_note",
                },
            )
            response.raise_for_status()

        logger.info(
            "Added private note to HappyFox ticket",
            extra={"hf_ticket_id": ticket_id, "action": "add_staff_note"},
        )
        return response.json()

    def get_synced_comment_ids(self, ticket_id: int) -> set[int]:
        """
        Get the set of ADO comment IDs already synced to a HappyFox ticket.

        Synced comments include a visible text marker: [ADO-Comment-{id}]
        (HappyFox strips HTML comments, so we use visible text instead.)
        This scans all ticket messages for those markers to prevent duplicates.

        Returns:
            Set of ADO comment IDs that have already been synced.
        """
        import re

        try:
            data = self._fetch_ticket_data(ticket_id)

            synced_ids: set[int] = set()
            for update in data.get("updates", []):
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                html = message.get("html", "") or ""
                text = message.get("text", "") or ""
                # Search both html and text for marker pattern
                for content in (html, text):
                    for match in re.finditer(r"\[ADO-Comment-(\d+)\]", content):
                        synced_ids.add(int(match.group(1)))
            return synced_ids
        except Exception as e:
            logger.warning(
                "Could not fetch synced comment IDs for dedup",
                extra={"hf_ticket_id": ticket_id, "error": str(e)},
            )
            return set()

    def _get_default_staff_id(self) -> int | None:
        """
        Get the staff user ID for the integration service account (Avaratak).

        Fetches the staff list once, searches for the Avaratak account by name
        or email, and caches the result. Falls back to None if not found.
        """
        if self._staff_id_fetched:
            return self._default_staff_id

        self._staff_id_fetched = True
        try:
            url = f"{self._base_url}/staff/"
            response = self._client.get(url)
            response.raise_for_status()
            staff_list = response.json()

            if staff_list and isinstance(staff_list, list):
                # Look for the integration service account by name or email
                # Patterns are configurable via HF_STAFF_MATCH_PATTERNS App Setting
                match_patterns = self._settings.get_staff_match_patterns()
                for staff in staff_list:
                    name = staff.get("name", "").lower()
                    email = staff.get("email", "").lower()
                    if any(p in email or p in name for p in match_patterns):
                        self._default_staff_id = staff.get("id")
                        logger.info(
                            "Found Avaratak staff account",
                            extra={
                                "staff_id": self._default_staff_id,
                                "staff_name": staff.get("name"),
                                "staff_email": staff.get("email"),
                            },
                        )
                        return self._default_staff_id

                # If not found, log all available staff for debugging
                staff_names = [
                    f"{s.get('name', '?')} (id={s.get('id')})"
                    for s in staff_list[:10]
                ]
                logger.warning(
                    "Avaratak staff account not found — updates will proceed without staff ID",
                    extra={"available_staff": staff_names, "staff_count": len(staff_list)},
                )
        except Exception as e:
            logger.warning(
                "Could not fetch HappyFox staff list — updates will proceed without staff ID",
                extra={"error": str(e)},
            )

        return self._default_staff_id

    # --- Metadata fetchers (cached) ---

    def get_statuses(self, *, force_refresh: bool = False) -> list[HappyFoxStatusResponse]:
        """Fetch and cache HappyFox statuses."""
        if self._statuses is None or force_refresh:
            url = f"{self._base_url}/statuses/"
            response = self._client.get(url)
            response.raise_for_status()
            self._statuses = [HappyFoxStatusResponse.model_validate(s) for s in response.json()]
            logger.info("Cached HappyFox statuses", extra={"count": len(self._statuses)})
        return self._statuses

    def get_priorities(self, *, force_refresh: bool = False) -> list[HappyFoxPriorityResponse]:
        """Fetch and cache HappyFox priorities."""
        if self._priorities is None or force_refresh:
            url = f"{self._base_url}/priorities/"
            response = self._client.get(url)
            response.raise_for_status()
            self._priorities = [HappyFoxPriorityResponse.model_validate(p) for p in response.json()]
            logger.info("Cached HappyFox priorities", extra={"count": len(self._priorities)})
        return self._priorities

    def get_categories(self, *, force_refresh: bool = False) -> list[HappyFoxCategoryResponse]:
        """Fetch and cache HappyFox categories."""
        if self._categories is None or force_refresh:
            url = f"{self._base_url}/categories/"
            response = self._client.get(url)
            response.raise_for_status()
            self._categories = [HappyFoxCategoryResponse.model_validate(c) for c in response.json()]
            logger.info("Cached HappyFox categories", extra={"count": len(self._categories)})
        return self._categories

    @retry_happyfox_api
    def find_ticket_by_ado_child_id(self, ado_child_id: str) -> HappyFoxTicketResponse | None:
        """
        Search HappyFox for a ticket linked to a specific ADO child work item ID.

        Uses the HappyFox search API to look up tickets by the "DEV Ticket Number"
        custom field (HF field id=29, API key: t-cf-29).

        Args:
            ado_child_id: The ADO Client Story Work Item ID (as string).

        Returns:
            HappyFoxTicketResponse if found, None if no match.
        """
        url = f"{self._base_url}/tickets/"
        # HappyFox search: look up by the "DEV Ticket Number" custom field (field id=29)
        params = {
            "q": f'"DEV Ticket Number":"{ado_child_id}"',
            "size": 1,
        }

        response = self._client.get(url, params=params)
        self._check_rate_limit(response)
        response.raise_for_status()

        data = response.json()

        # HappyFox returns a paginated response with a "data" key
        tickets = data if isinstance(data, list) else data.get("data", [])

        if not tickets:
            logger.debug(
                "No HappyFox ticket found for ADO child ID",
                extra={"ado_child_id": ado_child_id, "action": "find_by_ado_child_id"},
            )
            return None

        ticket = HappyFoxTicketResponse.model_validate(tickets[0])
        logger.info(
            "Found existing HappyFox ticket for ADO child ID",
            extra={
                "ado_child_id": ado_child_id,
                "hf_ticket_id": ticket.id,
                "action": "find_by_ado_child_id",
            },
        )
        return ticket

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
