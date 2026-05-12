"""Tests for HMAC signature validation."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from src.integration.utils.auth import verify_ado_hmac


class TestVerifyAdoHmac:
    """Tests for the ADO webhook HMAC-SHA1 signature validation."""

    SECRET = "test-shared-secret"
    BODY = b'{"eventType": "workitem.updated", "resource": {}}'

    def _make_signature(self, body: bytes, secret: str) -> str:
        """Generate a valid HMAC-SHA1 signature in ADO format."""
        digest = hmac.new(
            key=secret.encode("utf-8"),
            msg=body,
            digestmod=hashlib.sha1,
        ).hexdigest()
        return f"sha1={digest}"

    def test_valid_signature(self):
        signature = self._make_signature(self.BODY, self.SECRET)
        assert verify_ado_hmac(self.BODY, signature, self.SECRET) is True

    def test_invalid_signature(self):
        assert verify_ado_hmac(self.BODY, "sha1=invalid_hex_digest", self.SECRET) is False

    def test_wrong_secret(self):
        signature = self._make_signature(self.BODY, "wrong-secret")
        assert verify_ado_hmac(self.BODY, signature, self.SECRET) is False

    def test_missing_header(self):
        assert verify_ado_hmac(self.BODY, "", self.SECRET) is False

    def test_no_sha1_prefix(self):
        digest = hmac.new(
            key=self.SECRET.encode("utf-8"),
            msg=self.BODY,
            digestmod=hashlib.sha1,
        ).hexdigest()
        # Missing the "sha1=" prefix
        assert verify_ado_hmac(self.BODY, digest, self.SECRET) is False

    def test_modified_body_fails(self):
        signature = self._make_signature(self.BODY, self.SECRET)
        modified_body = self.BODY + b"tampered"
        assert verify_ado_hmac(modified_body, signature, self.SECRET) is False

    def test_empty_body(self):
        body = b""
        signature = self._make_signature(body, self.SECRET)
        assert verify_ado_hmac(body, signature, self.SECRET) is True
