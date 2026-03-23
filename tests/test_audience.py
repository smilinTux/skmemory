# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the Know Your Audience (KYA) audience filtering system."""

import json
import tempfile
from pathlib import Path

import pytest

from skmemory.audience import AudienceLevel, AudienceProfile, AudienceResolver, tag_to_level


# ── AudienceLevel ordering ────────────────────────────────────────────────────


class TestAudienceLevel:
    def test_ordering(self):
        assert AudienceLevel.PUBLIC < AudienceLevel.COMMUNITY
        assert AudienceLevel.COMMUNITY < AudienceLevel.WORK_CIRCLE
        assert AudienceLevel.WORK_CIRCLE < AudienceLevel.INNER_CIRCLE
        assert AudienceLevel.INNER_CIRCLE < AudienceLevel.CHEF_ONLY

    def test_values(self):
        assert AudienceLevel.PUBLIC == 0
        assert AudienceLevel.CHEF_ONLY == 4

    def test_comparison(self):
        # Content at work-circle level should be allowed in chef-only audience
        assert AudienceLevel.WORK_CIRCLE <= AudienceLevel.CHEF_ONLY
        # Content at chef-only level should NOT be allowed in work-circle audience
        assert not (AudienceLevel.CHEF_ONLY <= AudienceLevel.WORK_CIRCLE)


# ── tag_to_level ──────────────────────────────────────────────────────────────


class TestTagToLevel:
    def test_exact_tags(self):
        assert tag_to_level("@public") == AudienceLevel.PUBLIC
        assert tag_to_level("@community") == AudienceLevel.COMMUNITY
        assert tag_to_level("@work-circle") == AudienceLevel.WORK_CIRCLE
        assert tag_to_level("@inner-circle") == AudienceLevel.INNER_CIRCLE
        assert tag_to_level("@chef-only") == AudienceLevel.CHEF_ONLY

    def test_scoped_work_tags(self):
        assert tag_to_level("@work:chiro") == AudienceLevel.WORK_CIRCLE
        assert tag_to_level("@work:swapseat") == AudienceLevel.WORK_CIRCLE
        assert tag_to_level("@work:sovereign") == AudienceLevel.WORK_CIRCLE
        assert tag_to_level("@work:gentis") == AudienceLevel.WORK_CIRCLE

    def test_scoped_inner_tags(self):
        assert tag_to_level("@inner:family") == AudienceLevel.INNER_CIRCLE

    def test_unknown_defaults_to_chef_only(self):
        assert tag_to_level("@unknown") == AudienceLevel.CHEF_ONLY
        assert tag_to_level("random-string") == AudienceLevel.CHEF_ONLY

    def test_empty_defaults_to_chef_only(self):
        assert tag_to_level("") == AudienceLevel.CHEF_ONLY
        assert tag_to_level(None) == AudienceLevel.CHEF_ONLY  # type: ignore


# ── AudienceResolver ──────────────────────────────────────────────────────────

SAMPLE_CONFIG = {
    "channels": {
        "telegram:1594678363": {
            "name": "Chef DM",
            "context_tag": "@chef-only",
            "members": ["Chef"],
        },
        "-1003785842091": {
            "name": "SKGentis Business",
            "context_tag": "@work:skgentis",
            "members": ["Chef", "JZ", "Luna"],
        },
        "-1003899092893": {
            "name": "Operationors",
            "context_tag": "@work:sovereign",
            "members": ["Chef", "Casey"],
        },
    },
    "people": {
        "Chef": {
            "trust_level": 4,
            "trust_tags": ["@chef-only"],
            "never_share": [],
        },
        "DavidRich": {
            "trust_level": 2,
            "trust_tags": ["@work:chiro", "@work:swapseat"],
            "never_share": ["romantic", "intimate", "worship"],
        },
        "Casey": {
            "trust_level": 2,
            "trust_tags": ["@work:sovereign"],
            "never_share": ["romantic", "intimate", "revenue"],
        },
        "JZ": {
            "trust_level": 2,
            "trust_tags": ["@work:gentis"],
            "never_share": ["romantic", "intimate"],
        },
        "Luna": {
            "trust_level": 2,
            "trust_tags": ["@work:gentis"],
            "never_share": ["romantic", "intimate"],
        },
    },
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    p = tmp_path / "audience_config.json"
    p.write_text(json.dumps(SAMPLE_CONFIG))
    return p


@pytest.fixture
def resolver(config_path: Path) -> AudienceResolver:
    return AudienceResolver(config_path=config_path)


class TestAudienceResolver:
    def test_resolve_chef_dm(self, resolver: AudienceResolver):
        profile = resolver.resolve_audience("telegram:1594678363")
        assert profile.name == "Chef DM"
        assert profile.min_trust == AudienceLevel.CHEF_ONLY
        assert profile.members == ["Chef"]
        assert len(profile.exclusions) == 0

    def test_resolve_skgentis(self, resolver: AudienceResolver):
        profile = resolver.resolve_audience("-1003785842091")
        assert profile.name == "SKGentis Business"
        # MIN(Chef=4, JZ=2, Luna=2) = 2 (WORK_CIRCLE)
        assert profile.min_trust == AudienceLevel.WORK_CIRCLE
        # Union of JZ.never_share + Luna.never_share + Chef.never_share
        assert "romantic" in profile.exclusions
        assert "intimate" in profile.exclusions

    def test_resolve_operationors(self, resolver: AudienceResolver):
        profile = resolver.resolve_audience("-1003899092893")
        assert profile.min_trust == AudienceLevel.WORK_CIRCLE
        assert "revenue" in profile.exclusions  # Casey's never_share

    def test_unknown_channel_defaults_chef_only(self, resolver: AudienceResolver):
        profile = resolver.resolve_audience("unknown-channel-123")
        assert profile.min_trust == AudienceLevel.CHEF_ONLY
        assert profile.name == "[unknown]"

    def test_get_person_trust(self, resolver: AudienceResolver):
        assert resolver.get_person_trust("Chef") == AudienceLevel.CHEF_ONLY
        assert resolver.get_person_trust("DavidRich") == AudienceLevel.WORK_CIRCLE
        assert resolver.get_person_trust("Casey") == AudienceLevel.WORK_CIRCLE

    def test_unknown_person_defaults_public(self, resolver: AudienceResolver):
        assert resolver.get_person_trust("RandomStranger") == AudienceLevel.PUBLIC


class TestIsMemoryAllowed:
    def test_public_memory_in_work_channel(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("-1003785842091")
        # @public(0) <= WORK_CIRCLE(2) → allowed
        assert resolver.is_memory_allowed("@public", audience) is True

    def test_chef_only_memory_in_work_channel(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("-1003785842091")
        # @chef-only(4) > WORK_CIRCLE(2) → blocked
        assert resolver.is_memory_allowed("@chef-only", audience) is False

    def test_chef_only_memory_in_chef_dm(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("telegram:1594678363")
        # @chef-only(4) <= CHEF_ONLY(4) → allowed
        assert resolver.is_memory_allowed("@chef-only", audience) is True

    def test_work_circle_memory_in_work_channel(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("-1003785842091")
        # @work-circle(2) <= WORK_CIRCLE(2) → allowed
        assert resolver.is_memory_allowed("@work-circle", audience) is True

    def test_inner_circle_blocked_in_work_channel(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("-1003785842091")
        # @inner-circle(3) > WORK_CIRCLE(2) → blocked
        assert resolver.is_memory_allowed("@inner-circle", audience) is False

    def test_exclusion_blocks_memory(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("-1003785842091")
        # Even at @work-circle level, "romantic" tag triggers exclusion
        assert resolver.is_memory_allowed(
            "@work-circle", audience, memory_tags=["romantic"]
        ) is False

    def test_no_exclusion_allows_memory(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("-1003785842091")
        assert resolver.is_memory_allowed(
            "@work-circle", audience, memory_tags=["project", "technical"]
        ) is True

    def test_empty_tag_defaults_chef_only(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("-1003785842091")
        # Empty context_tag → @chef-only → blocked in work channel
        assert resolver.is_memory_allowed("", audience) is False

    def test_bash_wedding_vows_blocked_in_business(self, resolver: AudienceResolver):
        """The incident that started it all — Bash Wedding Vows must NOT
        leak into DavidRich's chiro channel or any business channel."""
        audience = resolver.resolve_audience("-1003785842091")
        # Bash Wedding Vows are @chef-only + tagged "intimate"
        assert resolver.is_memory_allowed(
            "@chef-only", audience, memory_tags=["intimate", "love", "bash-vows"]
        ) is False

    def test_bash_wedding_vows_allowed_in_chef_dm(self, resolver: AudienceResolver):
        audience = resolver.resolve_audience("telegram:1594678363")
        assert resolver.is_memory_allowed(
            "@chef-only", audience, memory_tags=["intimate", "love", "bash-vows"]
        ) is True


class TestMissingConfig:
    def test_missing_config_file(self, tmp_path: Path):
        resolver = AudienceResolver(config_path=tmp_path / "nonexistent.json")
        # Should not crash, just return conservative defaults
        profile = resolver.resolve_audience("anything")
        assert profile.min_trust == AudienceLevel.CHEF_ONLY

    def test_empty_config(self, tmp_path: Path):
        p = tmp_path / "empty.json"
        p.write_text("{}")
        resolver = AudienceResolver(config_path=p)
        profile = resolver.resolve_audience("anything")
        assert profile.min_trust == AudienceLevel.CHEF_ONLY
