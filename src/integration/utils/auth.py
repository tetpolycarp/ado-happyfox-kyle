"""
HMAC signature validation for ADO Service Hook webhooks.

ADO Service Hooks sign the raw request body using HMAC-SHA1 with a shared secret.
The signature is sent in the X-Hub-Signature header.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def verify_ado_hmac(body: bytes, signature_header: str, secret: str) -> bool:
    """
    Validate an ADO Service Hook HMAC-SHA1 signature.

    Args:
        body: Raw request body bytes (must not be parsed/modified before validation).
        signature_header: Value of the X-Hub-Signature header from the request.
        secret: The shared secret configured in the ADO Service Hook.

    Returns:
        True if the signature is valid, False otherwise.
    """
    if not signature_header:
        logger.warning("Missing X-Hub-Signature header")
        return False

    # ADO sends the signature as "sha1=<hex_digest>"
    if not signature_header.startswith("sha1="):
        logger.warning("Unexpected signature format: does not start with 'sha1='")
        return False

    expected_signature = signature_header[5:]  # Strip "sha1=" prefix

    computed = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha1,
    ).hexdigest()

    is_valid = hmac.compare_digest(computed, expected_signature)

    if not is_valid:
        logger.warning("HMAC signature mismatch on ADO webhook")

    return is_valid
