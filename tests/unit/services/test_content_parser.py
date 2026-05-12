"""Tests for the tagged content parser."""

import pytest

from src.integration.services.content_parser import (
    ParsedContent,
    TaggedSection,
    filter_content_for_client,
    get_client_aliases,
    parse_tagged_content,
)


# ---------------------------------------------------------------------------
# Alias map used across tests
# ---------------------------------------------------------------------------

ALIAS_MAP = {
    "GOF": "Florida Fish and Wildlife Conservation Commission (GOF)",
    "ADCNR": "Alabama State Parks (ADCNR)",
    "CPW": "Colorado Parks and Wildlife (CPW)",
    "SDGFP": "South Dakota - Division of Parks & Recreation (SDGFP)",
}


# ---------------------------------------------------------------------------
# parse_tagged_content
# ---------------------------------------------------------------------------


class TestParseTaggedContent:
    def test_no_tags_returns_full_content(self):
        content = "As a user, I need a feature that works."
        result = parse_tagged_content(content)
        assert result.has_tags is False
        assert result.preamble == content
        assert result.sections == []

    def test_empty_content(self):
        result = parse_tagged_content("")
        assert result.has_tags is False
        assert result.preamble == ""

    def test_none_content(self):
        result = parse_tagged_content("")
        assert result.has_tags is False

    def test_standard_tag_only(self):
        content = "[STANDARD]\nAs a user, I need this for everyone."
        result = parse_tagged_content(content)
        assert result.has_tags is True
        assert len(result.sections) == 1
        assert result.sections[0].tag == "STANDARD"
        assert "everyone" in result.sections[0].content

    def test_standard_and_client_tags(self):
        content = (
            "Internal notes here.\n\n"
            "[STANDARD]\n"
            "Standard content for all.\n\n"
            "[GOF]\n"
            "Florida-specific content.\n\n"
            "[ADCNR]\n"
            "Alabama-specific content."
        )
        result = parse_tagged_content(content)
        assert result.has_tags is True
        assert "Internal notes" in result.preamble
        assert len(result.sections) == 3
        assert result.sections[0].tag == "STANDARD"
        assert result.sections[1].tag == "GOF"
        assert result.sections[2].tag == "ADCNR"

    def test_tags_are_case_insensitive_in_output(self):
        content = "[standard]\nLowercase tag content."
        result = parse_tagged_content(content)
        assert result.has_tags is True
        assert result.sections[0].tag == "STANDARD"

    def test_preamble_excluded_when_tags_exist(self):
        content = (
            "This is product team safe space.\n\n"
            "[STANDARD]\n"
            "Real content."
        )
        result = parse_tagged_content(content)
        assert result.has_tags is True
        assert "safe space" in result.preamble
        # preamble should NOT be in any section
        for section in result.sections:
            assert "safe space" not in section.content

    def test_html_wrapped_tags(self):
        content = (
            "<p>Internal notes</p>\n"
            "<p>[STANDARD]</p>\n"
            "<p>Standard content here.</p>\n"
            "<p>[GOF]</p>\n"
            "<p>Florida content.</p>"
        )
        result = parse_tagged_content(content)
        assert result.has_tags is True
        assert len(result.sections) == 2
        assert result.sections[0].tag == "STANDARD"
        assert result.sections[1].tag == "GOF"

    def test_multiple_client_tags(self):
        content = (
            "[STANDARD]\nBase feature.\n\n"
            "[GOF]\nFlorida extras.\n\n"
            "[CPW]\nColorado extras.\n\n"
            "[ADCNR]\nAlabama extras."
        )
        result = parse_tagged_content(content)
        assert result.has_tags is True
        assert len(result.sections) == 4
        tags = [s.tag for s in result.sections]
        assert tags == ["STANDARD", "GOF", "CPW", "ADCNR"]


# ---------------------------------------------------------------------------
# get_client_aliases
# ---------------------------------------------------------------------------


class TestGetClientAliases:
    def test_finds_alias_from_map(self):
        aliases = get_client_aliases(
            "Florida Fish and Wildlife Conservation Commission (GOF)",
            ALIAS_MAP,
        )
        assert "GOF" in aliases

    def test_extracts_abbreviation_from_parentheses(self):
        aliases = get_client_aliases(
            "Florida Fish and Wildlife Conservation Commission (GOF)",
            {},  # empty map
        )
        assert "GOF" in aliases

    def test_includes_full_name(self):
        aliases = get_client_aliases("Alabama State Parks (ADCNR)", ALIAS_MAP)
        assert "ALABAMA STATE PARKS (ADCNR)" in aliases
        assert "ADCNR" in aliases

    def test_no_parentheses_in_name(self):
        aliases = get_client_aliases("Lake Casitas Municipal Water District", {})
        # Full name should be there, but no abbreviation
        assert "LAKE CASITAS MUNICIPAL WATER DISTRICT" in aliases

    def test_case_insensitive_map_match(self):
        map_with_case = {"gof": "Florida Fish and Wildlife Conservation Commission (GOF)"}
        aliases = get_client_aliases(
            "Florida Fish and Wildlife Conservation Commission (GOF)",
            map_with_case,
        )
        assert "GOF" in aliases


# ---------------------------------------------------------------------------
# filter_content_for_client
# ---------------------------------------------------------------------------


class TestFilterContentForClient:
    def test_no_tags_returns_everything(self):
        content = "As a user, I need a feature."
        result = filter_content_for_client(content, "Any Client (AC)", ALIAS_MAP)
        assert result == content

    def test_standard_only_all_clients_get_it(self):
        content = "[STANDARD]\nEveryone gets this."
        result = filter_content_for_client(
            content,
            "Colorado Parks and Wildlife (CPW)",
            ALIAS_MAP,
        )
        assert "Everyone gets this" in result

    def test_client_specific_content_included(self):
        content = (
            "[STANDARD]\nBase feature.\n\n"
            "[GOF]\nFlorida-specific feature."
        )
        result = filter_content_for_client(
            content,
            "Florida Fish and Wildlife Conservation Commission (GOF)",
            ALIAS_MAP,
        )
        assert "Base feature" in result
        assert "Florida-specific" in result

    def test_other_client_content_excluded(self):
        content = (
            "[STANDARD]\nBase feature.\n\n"
            "[GOF]\nFlorida-specific feature.\n\n"
            "[ADCNR]\nAlabama-specific feature."
        )
        result = filter_content_for_client(
            content,
            "Colorado Parks and Wildlife (CPW)",
            ALIAS_MAP,
        )
        assert "Base feature" in result
        assert "Florida-specific" not in result
        assert "Alabama-specific" not in result

    def test_preamble_excluded_when_tags_exist(self):
        content = (
            "Product team internal notes.\n\n"
            "[STANDARD]\nReal content."
        )
        result = filter_content_for_client(
            content,
            "Alabama State Parks (ADCNR)",
            ALIAS_MAP,
        )
        assert "internal notes" not in result
        assert "Real content" in result

    def test_empty_content(self):
        result = filter_content_for_client("", "Any Client", ALIAS_MAP)
        assert result == ""

    def test_client_with_no_matching_tags_gets_standard_only(self):
        content = (
            "[STANDARD]\nBase content.\n\n"
            "[GOF]\nFlorida only."
        )
        result = filter_content_for_client(
            content,
            "West Virginia DNR (WVDNR)",
            ALIAS_MAP,
        )
        assert "Base content" in result
        assert "Florida only" not in result

    def test_multiple_client_sections_for_same_client(self):
        """If someone adds two sections with the same tag, both should be included."""
        content = (
            "[STANDARD]\nBase.\n\n"
            "[GOF]\nFlorida part 1.\n\n"
            "[STANDARD]\nMore base.\n\n"
            "[GOF]\nFlorida part 2."
        )
        result = filter_content_for_client(
            content,
            "Florida Fish and Wildlife Conservation Commission (GOF)",
            ALIAS_MAP,
        )
        assert "Base" in result
        assert "Florida part 1" in result
        assert "More base" in result
        assert "Florida part 2" in result

    def test_html_content_with_tags(self):
        content = (
            "<p>Internal product notes</p>\n"
            "<p>[STANDARD]</p>\n"
            "<p>As a user, I need <strong>this feature</strong></p>\n"
            "<p>[GOF]</p>\n"
            "<p>Florida needs manatee tracking</p>"
        )
        result = filter_content_for_client(
            content,
            "Florida Fish and Wildlife Conservation Commission (GOF)",
            ALIAS_MAP,
        )
        assert "this feature" in result
        assert "manatee tracking" in result
        assert "Internal product" not in result

    def test_backward_compatible_no_tags_html(self):
        """Existing content without tags should pass through unchanged."""
        content = "<p>As a user, I need a feature.</p><p>More details here.</p>"
        result = filter_content_for_client(content, "Any Client (AC)", ALIAS_MAP)
        assert result == content
