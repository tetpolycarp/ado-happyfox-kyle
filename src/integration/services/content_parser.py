"""
Tagged content parser for client-specific field filtering.

ADO content fields (Description, Acceptance Criteria, Test Scenarios) can
contain tagged sections that target specific clients. This module parses
those tags and returns only the content relevant to a given client.

TAG FORMAT:
    [STANDARD]   — content included for ALL clients
    [ALIAS]      — content included only for the client matching that alias

RULES:
    - If NO tags exist in the content, NOTHING is copied to child work items.
      Only explicitly tagged content transfers.
    - If tags exist, only [STANDARD] sections and sections matching the client's
      alias are returned. Content outside any tag (preamble) is EXCLUDED
      (treated as internal/product team notes).
    - Tags are matched case-insensitively.
    - Multiple client-specific tags can appear in a single field.
    - Tags can appear on their own line or inline. Content runs from one tag
      to the next tag (or end of string).

ALIAS MAPPING:
    Short aliases (e.g., "GOF") are mapped to full client names via the
    MAPPING_CLIENT_ALIAS App Setting. The mapping is bidirectional: given
    a full client name, we find its alias(es) to match against tags.

Example:
    Input:
        This is internal product team notes — NOT synced.

        [STANDARD]
        As a user, I need this feature for all clients.

        [GOF]
        Florida-specific: add manatee species to the list.

        [ADCNR]
        Alabama-specific: add deer season logic.

    For client "Florida Fish and Wildlife Conservation Commission (GOF)":
        → Returns [STANDARD] + [GOF] content

    For client "Alabama State Parks (ADCNR)":
        → Returns [STANDARD] + [ADCNR] content

    For client "Colorado Parks and Wildlife (CPW)":
        → Returns [STANDARD] content only
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Matches a tag like [STANDARD], [GOF], [ADCNR], [Florida GOF], etc.
# Tags must be on their own line (possibly with whitespace) or at the start
# of a line. The tag name is captured in group 1.
# Supports tags in plain text AND inside HTML (e.g., <p>[STANDARD]</p>).
_TAG_PATTERN = re.compile(
    # Prefix only consumes common OPENING paragraph wrappers (<div>/<p>/<span>).
    # Deliberately does NOT eat closing tags or list tags — those belong to
    # the preceding section's content and must stay with it so bullet/list
    # formatting is preserved.
    r"(?:^|\n)\s*(?:<(?:div|p|span)\b[^>]*>\s*)*\[([A-Za-z0-9_ /&,().-]+)\]",
    re.MULTILINE | re.IGNORECASE,
)

# Block-level HTML tags. ADO returns rich-text fields as a single HTML blob
# with no real newlines (e.g. "<div>preamble</div><div>[STANDARD]</div>..."),
# which prevents the tag regex from finding line boundaries. We inject \n
# before and after these tags so tags always have a clean `\n` boundary.
# Inline formatting (<b>, <i>, <span>, etc.) is left alone.
_BLOCK_TAG_PATTERN = re.compile(
    r"(</?(?:div|p|br|h[1-6]|li|ul|ol|tr|table|thead|tbody|tfoot)\b[^>]*/?>)",
    re.IGNORECASE,
)


def _normalize_html_for_parsing(raw: str) -> str:
    """
    Inject newlines around block-level HTML tags so the tag regex can detect
    [TAG] markers inside ADO rich-text HTML. The HTML tags themselves remain
    intact so downstream ADO rendering is unaffected.
    """
    if not raw:
        return raw
    return _BLOCK_TAG_PATTERN.sub(lambda m: "\n" + m.group(1) + "\n", raw)


def _collapse_injected_newlines(s: str) -> str:
    """
    Remove newlines that were injected by _normalize_html_for_parsing.

    The normalization step adds ``\\n`` before and after block-level HTML
    tags so the tag regex can match line boundaries. Those artificial
    newlines must be stripped back out before the content is written to
    a child work item — otherwise ADO renders them as extra paragraph
    spacing (especially visible between ``<li>`` elements in bullet lists).
    """
    if not s:
        return s
    # Remove \n immediately before or after any HTML tag.
    # This is safe because ADO's rich-text blob has no meaningful
    # plain-text newlines between HTML tags — all spacing is driven by
    # the tags themselves.
    s = re.sub(r"\n+(<[^>]+>)", r"\1", s)
    s = re.sub(r"(<[^>]+>)\n+", r"\1", s)
    # Collapse any remaining multi-newline runs (from between tags that
    # were both removed) into a single newline.
    s = re.sub(r"\n{2,}", "\n", s)
    return s


def _clean_fragment(s: str) -> str:
    """
    Trim unbalanced HTML artifacts from the start/end of an extracted
    section. After regex-splitting on tag boundaries, leading/trailing
    block-level tags (open or close) may remain stranded — these render
    as noise if fed back to ADO. Strip them conservatively, then balance
    the fragment by removing trailing close tags that have no matching
    open within the fragment itself.

    Also collapses the artificial newlines injected by
    _normalize_html_for_parsing so bullet-list and other block-level
    formatting is preserved without extra spacing.
    """
    if not s:
        return s
    block = r"(?:div|p|br|li|ul|ol|h[1-6]|tr|table|thead|tbody|tfoot)"

    # Strip leading close tags (and self-closing <br>) + whitespace
    leading = re.compile(r"^(?:\s|<br\s*/?>|</" + block + r">)+", re.IGNORECASE)
    s = leading.sub("", s)
    # Strip trailing open tags + whitespace
    trailing_open = re.compile(r"(?:\s|<" + block + r"\b[^>]*>)+$", re.IGNORECASE)
    s = trailing_open.sub("", s)

    # Balance: remove trailing close tags whose opens don't appear in the fragment
    while True:
        m = re.search(r"</(" + block + r")>\s*$", s, re.IGNORECASE)
        if not m:
            break
        tag_name = m.group(1).lower()
        open_re = re.compile(r"<" + re.escape(tag_name) + r"\b[^>]*>", re.IGNORECASE)
        opens = len(open_re.findall(s[: m.start()]))
        closes = len(re.findall(r"</" + re.escape(tag_name) + r">", s[: m.start()], re.IGNORECASE))
        if opens > closes:
            break  # This close balances an earlier open — keep it
        s = s[: m.start()] + s[m.end():]

    # Balance: remove leading open tags whose closes don't appear in the fragment.
    # Middle sections often inherit an unclosed <div> because the prior match's
    # prefix consumed the closing </div>. Strip those to avoid malformed HTML.
    while True:
        m = re.match(r"\s*<(" + block + r")\b[^>]*>", s, re.IGNORECASE)
        if not m:
            break
        tag_name = m.group(1).lower()
        close_re = re.compile(r"</" + re.escape(tag_name) + r">", re.IGNORECASE)
        closes = len(close_re.findall(s[m.end():]))
        opens = len(re.findall(r"<" + re.escape(tag_name) + r"\b[^>]*>", s[m.end():], re.IGNORECASE))
        if closes > opens:
            break  # This open has a matching close later — keep it
        s = s[m.end():]

    # Collapse artificial newlines injected by _normalize_html_for_parsing
    # so bullet lists and other block formatting render without extra spacing.
    s = _collapse_injected_newlines(s)

    return s.strip()


@dataclass
class TaggedSection:
    """A section of content with its associated tag."""

    tag: str  # Normalized to uppercase (e.g., "STANDARD", "GOF")
    content: str  # The content text following the tag


@dataclass
class ParsedContent:
    """Result of parsing a tagged content field."""

    has_tags: bool  # Whether any tags were found
    preamble: str  # Content before the first tag (excluded when tags exist)
    sections: list[TaggedSection] = field(default_factory=list)


def parse_tagged_content(raw_content: str) -> ParsedContent:
    """
    Parse a content field into tagged sections.

    Returns a ParsedContent with has_tags=False if no tags found (meaning
    the entire content should be used for all clients).
    """
    if not raw_content or not raw_content.strip():
        return ParsedContent(has_tags=False, preamble="")

    # Normalize HTML block tags → newlines so [TAG] markers inside ADO's
    # single-line HTML blobs are detectable by the regex.
    normalized = _normalize_html_for_parsing(raw_content)

    matches = list(_TAG_PATTERN.finditer(normalized))

    if not matches:
        return ParsedContent(has_tags=False, preamble=raw_content)

    # Content before the first tag
    preamble = _clean_fragment(normalized[: matches[0].start()])

    sections: list[TaggedSection] = []
    for i, match in enumerate(matches):
        tag = match.group(1).strip().upper()
        # Content runs from end of this tag to start of next tag (or end of string)
        content_start = match.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(normalized)
        content = _clean_fragment(normalized[content_start:content_end])

        if content:
            sections.append(TaggedSection(tag=tag, content=content))

    return ParsedContent(has_tags=True, preamble=preamble, sections=sections)


def get_client_aliases(
    client_name: str,
    alias_map: dict[str, str],
) -> set[str]:
    """
    Get all aliases that match a given client name.

    The alias_map is {alias: full_client_name}. This function finds all
    aliases whose full_client_name matches the given client_name (case-insensitive).

    Also extracts the abbreviation in parentheses from the client name itself
    (e.g., "Florida Fish and Wildlife Conservation Commission (GOF)" → "GOF").

    Returns a set of uppercase alias strings.
    """
    aliases: set[str] = set()

    client_lower = client_name.strip().lower()

    # 1. Exact match against alias_map full names
    for alias, full_name in alias_map.items():
        if full_name.strip().lower() == client_lower:
            aliases.add(alias.strip().upper())

    # 2. Substring fallback — covers the case where ClientRequested holds a
    #    short form like "Alabama" or "Muskingum" but the alias map values
    #    are full names like "Alabama State Parks (ADCNR)". Only runs when
    #    the exact-match lookup above didn't find anything.
    if not aliases:
        for alias, full_name in alias_map.items():
            full_lower = full_name.strip().lower()
            # Prefer prefix matches to limit false positives, but also allow
            # the short name to appear anywhere in the full name.
            if full_lower.startswith(client_lower) or client_lower in full_lower:
                aliases.add(alias.strip().upper())

    # 3. Extract abbreviation from parentheses in the client name itself
    #    e.g., "Florida Fish and Wildlife Conservation Commission (GOF)" → "GOF"
    paren_match = re.search(r"\(([A-Za-z0-9]+)\)\s*$", client_name)
    if paren_match:
        aliases.add(paren_match.group(1).upper())

    # 4. Also add the full client name as an alias (for exact tag matches)
    aliases.add(client_name.strip().upper())

    return aliases


def filter_content_for_client(
    raw_content: str,
    client_name: str,
    alias_map: dict[str, str],
) -> str:
    """
    Filter a tagged content field to return only sections relevant to a client.

    Args:
        raw_content: The raw HTML/text content from an ADO field.
        client_name: The full client name (e.g., from portal field or ClientRequested).
        alias_map: Mapping of {alias: full_client_name} from App Settings.

    Returns:
        The filtered content string. Only [STANDARD] and client-matching
        tagged sections are returned. If no tags exist, returns empty string
        — untagged content is never copied to child work items.
    """
    parsed = parse_tagged_content(raw_content)

    if not parsed.has_tags:
        # No tags at all → nothing to copy to children.
        # Only explicitly tagged content ([STANDARD], [CLIENT]) transfers.
        return ""

    # Determine which tags match this client
    client_aliases = get_client_aliases(client_name, alias_map)
    # STANDARD always matches
    matching_tags = {"STANDARD"} | client_aliases

    # Collect matching sections in order
    matched_sections: list[str] = []
    for section in parsed.sections:
        if section.tag in matching_tags:
            matched_sections.append(section.content)

    if not matched_sections:
        logger.warning(
            "No tagged content matched for client — returning empty",
            extra={
                "client": client_name,
                "aliases": list(client_aliases),
                "available_tags": [s.tag for s in parsed.sections],
            },
        )
        return ""

    # Separate sections with a visible line break. ADO rich-text fields
    # render as HTML, so <br/> guarantees a visual gap between the STANDARD
    # content and the client-specific content (plain `\n\n` would collapse).
    return "<br/><br/>".join(matched_sections)


# ---------------------------------------------------------------------------
# Inline image extraction and URL rewriting
# ---------------------------------------------------------------------------

# Matches <img> tags whose src points to an ADO attachment URL.
# ADO format: https://dev.azure.com/{org}/{project}/_apis/wit/attachments/{guid}
_ADO_IMG_PATTERN = re.compile(
    r"""<img\b([^>]*)\bsrc\s*=\s*["']"""
    r"""(https?://dev\.azure\.com/[^"']+)"""
    r"""["']([^>]*)/?""" r""">""",
    re.IGNORECASE,
)


def extract_ado_image_urls(html: str) -> list[dict[str, str]]:
    """
    Find all ``<img>`` tags whose ``src`` points to an ADO attachment URL.

    Returns a list of dicts with keys:
    - ``url``:      the full ADO attachment URL
    - ``filename``: the filename extracted from the URL's ``fileName``
                    query parameter, or a generated name from the GUID
    - ``full_tag``: the complete ``<img ...>`` match (for replacement)
    """
    if not html:
        return []

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for m in _ADO_IMG_PATTERN.finditer(html):
        ado_url = m.group(2)
        if ado_url in seen_urls:
            continue
        seen_urls.add(ado_url)

        # Try to extract filename from ?fileName=... query param
        filename = _extract_filename_from_url(ado_url)
        results.append({
            "url": ado_url,
            "filename": filename,
            "full_tag": m.group(0),
        })

    return results


def _extract_filename_from_url(url: str) -> str:
    """
    Extract a filename from an ADO attachment URL.

    Tries the ``fileName`` query parameter first, then falls back to
    the attachment GUID with a .png extension.
    """
    # Parse fileName=... from the query string
    fn_match = re.search(r"[?&]fileName=([^&]+)", url, re.IGNORECASE)
    if fn_match:
        from urllib.parse import unquote
        return unquote(fn_match.group(1))

    # Fallback: use the attachment ID from the URL path
    id_match = re.search(r"/attachments/([0-9a-zA-Z-]+)", url)
    if id_match:
        return f"{id_match.group(1)}.png"

    return "ado_image.png"


def rewrite_html_images(
    html: str,
    url_map: dict[str, str],
    failures: dict[str, str],
) -> str:
    """
    Replace ADO image URLs with HappyFox-hosted URLs.

    Args:
        html:     The HTML content with ADO ``<img>`` tags.
        url_map:  Mapping of {ado_url: hf_url} for successfully uploaded images.
        failures: Mapping of {ado_url: reason} for images that failed.

    Returns:
        The rewritten HTML. Successfully uploaded images get new ``src``
        attributes. Failed images are replaced with a visible text notice.
    """
    if not html:
        return html

    def _replace(m: re.Match) -> str:
        ado_url = m.group(2)

        if ado_url in url_map:
            # Replace src with the HF-hosted URL, keep other img attributes
            return f'<img {m.group(1)}src="{url_map[ado_url]}"{m.group(3)}>'

        if ado_url in failures:
            filename = _extract_filename_from_url(ado_url)
            reason = failures[ado_url]
            return (
                f'<em>[Image &quot;{filename}&quot; could not be copied: '
                f"{reason}]</em>"
            )

        # Unknown URL — leave as-is
        return m.group(0)

    return _ADO_IMG_PATTERN.sub(_replace, html)
