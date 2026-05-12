"""
Domain error hierarchy for the ADO ↔ HappyFox integration.

Define domain-specific errors here; map them to HTTP status codes at the function boundary.
"""

from __future__ import annotations


class IntegrationError(Exception):
    """Base for all integration errors."""


# --- ADO errors ---


class AdoApiError(IntegrationError):
    """Error communicating with the Azure DevOps REST API."""


class AdoWorkItemNotFoundError(AdoApiError):
    """Requested ADO work item does not exist."""


# --- HappyFox errors ---


class HappyFoxApiError(IntegrationError):
    """Error communicating with the HappyFox REST API."""


class HappyFoxTicketNotFoundError(HappyFoxApiError):
    """Requested HappyFox ticket does not exist."""


class HappyFoxRateLimitError(HappyFoxApiError):
    """HappyFox API rate limit exceeded (HTTP 429)."""


# --- Mapping errors ---


class MappingNotFoundError(IntegrationError):
    """No mapping record found for the given ID."""


