"""
Retry/backoff helpers for external API calls.

Uses tenacity for exponential backoff with jitter on transient failures.
Both ADO and HappyFox decorators share the same retry-on logic (transient
HTTP errors) with service-specific tuning for wait times.
"""

from __future__ import annotations

import logging
from typing import TypeVar

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.integration.errors import HappyFoxRateLimitError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient HTTP errors common to both services.
_TRANSIENT_HTTP = (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)


def _log_retry(retry_state: RetryCallState) -> None:
    """Log each retry attempt with useful context."""
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    logger.warning(
        "Retrying API call",
        extra={
            "attempt": retry_state.attempt_number,
            "exception_type": type(exception).__name__ if exception else None,
            "exception_msg": str(exception) if exception else None,
            "wait_seconds": retry_state.next_action.sleep if retry_state.next_action else None,  # type: ignore[union-attr]
        },
    )


def _make_retry(
    extra_exceptions: tuple = (),
    *,
    initial: int = 2,
    max_wait: int = 30,
    jitter: int = 2,
    attempts: int = 5,
):
    """Factory for service-specific retry decorators.

    Args:
        extra_exceptions: Additional exception types to retry on (beyond transient HTTP).
        initial: Initial backoff in seconds.
        max_wait: Maximum backoff in seconds.
        jitter: Random jitter range in seconds.
        attempts: Maximum number of attempts.
    """
    return retry(
        retry=retry_if_exception_type(_TRANSIENT_HTTP + extra_exceptions),
        wait=wait_exponential_jitter(initial=initial, max=max_wait, jitter=jitter),
        stop=stop_after_attempt(attempts),
        before_sleep=_log_retry,
        reraise=True,
    )


# ADO API: shorter backoff, no extra exceptions.
retry_ado_api = _make_retry(initial=2, max_wait=30, jitter=2)

# HappyFox API: longer backoff, also retries on rate-limit errors.
retry_happyfox_api = _make_retry(
    extra_exceptions=(HappyFoxRateLimitError,),
    initial=2,
    max_wait=60,
    jitter=5,
)
