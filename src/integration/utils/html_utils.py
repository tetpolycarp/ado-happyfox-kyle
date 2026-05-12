"""
Shared HTML normalization utilities for content comparison.

Used by both ado_service.py (parent→child field diff) and
child_story_processor (ADO→HF description diff) to ensure
identical normalization logic across all comparison paths.
"""

from __future__ import annotations

import html as _html_mod
import logging
import re as _re

logger = logging.getLogger(__name__)


def normalize_html(raw: str) -> str:
    """
    Strip HTML to plain text for content comparison.

    Applies:
    - Repeated HTML entity decoding (HappyFox can multi-encode)
    - <img> src preservation (replaced with ``[img:<src>]`` tokens so
      image additions/changes are detected as content differences)
    - HTML tag removal (all remaining tags)
    - Non-breaking space / zero-width space normalization
    - Carriage return removal
    - Whitespace collapse
    - Case-folding
    """
    if not raw:
        return ""
    text = raw
    for _ in range(3):
        decoded = _html_mod.unescape(text)
        if decoded == text:
            break
        text = decoded
    # Preserve <img> tags as text tokens so image changes are visible
    # to the comparison.  Replace <img ... src="URL" ...> with [img:URL].
    text = _re.sub(
        r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*/?>",
        r" [img:\1] ",
        text,
        flags=_re.IGNORECASE,
    )
    text = _re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00a0", " ").replace("\u200b", "").replace("\r", "")
    text = _re.sub(r"\s+", " ", text).strip()
    return text.lower()


def html_content_equal(a: str | None, b: str | None) -> bool:
    """Compare two HTML strings by their normalised text content."""
    return normalize_html(a or "") == normalize_html(b or "")


def html_content_differs(a: str, b: str) -> bool:
    """
    Compare two HTML strings, returning True if they differ meaningfully.

    Logs the first point of divergence to aid debugging false positives.
    """
    norm_a = normalize_html(a)
    norm_b = normalize_html(b)
    if norm_a != norm_b:
        _log_first_diff(norm_a, norm_b)
        return True
    return False


def _log_first_diff(text_a: str, text_b: str) -> None:
    """Log the first point where two normalized texts diverge."""
    max_ctx = 80
    for i, (ca, cb) in enumerate(zip(text_a, text_b)):
        if ca != cb:
            start = max(0, i - 20)
            logger.warning(
                "Content diff at char %d — A: ...%r... | B: ...%r...",
                i,
                text_a[start : i + max_ctx],
                text_b[start : i + max_ctx],
            )
            return
    shorter, longer = ("A", "B") if len(text_a) < len(text_b) else ("B", "A")
    logger.warning(
        "Content diff: %s is %d chars longer — trailing: %r",
        longer,
        abs(len(text_a) - len(text_b)),
        (text_a if len(text_a) > len(text_b) else text_b)[min(len(text_a), len(text_b)):],
    )
