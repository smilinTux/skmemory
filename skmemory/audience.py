# SPDX-License-Identifier: GPL-3.0-or-later
"""
Know Your Audience (KYA) — Audience-aware memory filtering for SKMemory.

Prevents private/intimate content from leaking into the wrong channels
during rehydration and message dispatch.

The five-level trust hierarchy:

    @public (0)        — Anyone on the internet
    @community (1)     — Known community members
    @work-circle (2)   — Business collaborators (professional trust)
    @inner-circle (3)  — Close friends / family (personal trust)
    @chef-only (4)     — Intimate, private, full-trust (Chef ONLY)

Conservative default: unknown channel = CHEF_ONLY, unknown tag = CHEF_ONLY.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("skmemory.audience")

# Default config shipped with the package
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "data" / "audience_config.json"

# Tag string → AudienceLevel mapping
_TAG_TO_LEVEL: dict[str, int] = {
    "@public": 0,
    "@community": 1,
    "@work-circle": 2,
    "@inner-circle": 3,
    "@chef-only": 4,
}


class AudienceLevel(IntEnum):
    """Five-level trust hierarchy for context-aware filtering.

    Higher values = more restrictive (fewer people allowed to see the content).
    Comparison semantics: content_level <= audience_level means "allowed".

    Examples::

        AudienceLevel.PUBLIC < AudienceLevel.CHEF_ONLY   # True
        AudienceLevel.WORK_CIRCLE >= AudienceLevel.COMMUNITY  # True
    """

    PUBLIC = 0
    COMMUNITY = 1
    WORK_CIRCLE = 2
    INNER_CIRCLE = 3
    CHEF_ONLY = 4


def tag_to_level(tag: str) -> AudienceLevel:
    """Convert a @context tag string to an AudienceLevel.

    Handles both exact tags (``@chef-only``) and scoped sub-tags
    (``@work:chiro`` → WORK_CIRCLE).  Unknown tags fall back to CHEF_ONLY
    (conservative default).

    Args:
        tag: The @context tag string.

    Returns:
        AudienceLevel: The resolved trust level.
    """
    if not tag:
        return AudienceLevel.CHEF_ONLY

    # Exact match first
    exact = _TAG_TO_LEVEL.get(tag)
    if exact is not None:
        return AudienceLevel(exact)

    # Scoped sub-tags: @work:* → WORK_CIRCLE, @inner:* → INNER_CIRCLE
    if tag.startswith("@work:"):
        return AudienceLevel.WORK_CIRCLE
    if tag.startswith("@inner:"):
        return AudienceLevel.INNER_CIRCLE

    # Unknown → conservative default
    logger.debug("Unknown context tag '%s', defaulting to CHEF_ONLY", tag)
    return AudienceLevel.CHEF_ONLY


@dataclass
class AudienceProfile:
    """The resolved audience for a specific channel.

    Attributes:
        channel_id: The channel identifier (e.g. ``telegram:1594678363``).
        name: Human-readable channel name.
        members: List of person names who can see this channel.
        min_trust: The effective trust ceiling — ``MIN(member.trust_level)``.
                   You're only as open as the least-trusted person in the room.
        exclusions: Set of content categories that are forbidden for any member
                    (union of all member ``never_share`` lists).
        context_tag: The primary @context tag for this channel.
    """

    channel_id: str
    name: str = ""
    members: list[str] = field(default_factory=list)
    min_trust: AudienceLevel = AudienceLevel.CHEF_ONLY
    exclusions: set[str] = field(default_factory=set)
    context_tag: str = "@chef-only"


class AudienceResolver:
    """Resolves audience profiles and checks memory access permissions.

    Loads configuration from a JSON file (audience_config.json) and
    provides methods to resolve channel audiences and check whether
    a memory is allowed for a given audience.

    Conservative defaults:
    - Unknown channel → CHEF_ONLY audience (nothing shown)
    - Unknown person → trust level 0 / PUBLIC access (treated as untrusted)
    - Memory with no context_tag → treat as @chef-only

    Args:
        config_path: Path to ``audience_config.json``.  If None, uses the
                     default config shipped with the package.
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(config_path) if config_path else _DEFAULT_CONFIG_PATH
        self._config: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """Load the audience config from disk.  Silently skips if missing."""
        if not self._config_path.exists():
            logger.warning(
                "Audience config not found at %s — using empty config", self._config_path
            )
            self._config = {"channels": {}, "people": {}}
            return
        try:
            self._config = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load audience config: %s", exc)
            self._config = {"channels": {}, "people": {}}

    def reload(self) -> None:
        """Reload the config from disk (useful after updates)."""
        self._load()

    # ── Channel resolution ────────────────────────────────────────────────────

    def resolve_audience(self, channel_id: str) -> AudienceProfile:
        """Resolve the audience profile for a channel.

        Returns a conservative (CHEF_ONLY) profile if the channel is unknown.

        Args:
            channel_id: The channel identifier.

        Returns:
            AudienceProfile: Resolved profile.
        """
        channels = self._config.get("channels", {})
        chan = channels.get(channel_id)

        if chan is None:
            # Unknown channel → maximum restriction
            logger.debug("Unknown channel '%s', defaulting to CHEF_ONLY", channel_id)
            return AudienceProfile(
                channel_id=channel_id,
                name="[unknown]",
                members=[],
                min_trust=AudienceLevel.CHEF_ONLY,
                exclusions=set(),
                context_tag="@chef-only",
            )

        members: list[str] = chan.get("members", [])
        context_tag: str = chan.get("context_tag", "@chef-only")

        # Compute effective trust = MIN(member trust levels)
        # If no members are listed, treat as chef-only
        if not members:
            min_trust = AudienceLevel.CHEF_ONLY
            exclusions: set[str] = set()
        else:
            trust_levels: list[AudienceLevel] = []
            all_exclusions: set[str] = set()
            for member_name in members:
                person = self._get_person(member_name)
                trust_levels.append(AudienceLevel(person.get("trust_level", 4)))
                all_exclusions.update(person.get("never_share", []))
            min_trust = min(trust_levels)
            exclusions = all_exclusions

        return AudienceProfile(
            channel_id=channel_id,
            name=chan.get("name", channel_id),
            members=members,
            min_trust=min_trust,
            exclusions=exclusions,
            context_tag=context_tag,
        )

    # ── Person lookup ─────────────────────────────────────────────────────────

    def _get_person(self, name: str) -> dict[str, Any]:
        """Return a person's config dict, or an empty dict if unknown."""
        return self._config.get("people", {}).get(name, {})

    def get_person_trust(self, name: str) -> AudienceLevel:
        """Get the trust level for a named person.

        Unknown persons default to PUBLIC (lowest trust — most conservative
        in terms of what they are *allowed* to receive).

        Args:
            name: Person's name as it appears in audience_config.json.

        Returns:
            AudienceLevel: The trust level for this person.
        """
        person = self._get_person(name)
        if not person:
            logger.debug("Unknown person '%s', defaulting to PUBLIC trust", name)
            return AudienceLevel.PUBLIC
        return AudienceLevel(person.get("trust_level", 4))

    # ── Memory access check ───────────────────────────────────────────────────

    def is_memory_allowed(
        self,
        memory_context_tag: str,
        audience: AudienceProfile,
        memory_tags: list[str] | None = None,
    ) -> bool:
        """Check whether content with the given context tag is allowed for an audience.

        A memory is allowed when **both** conditions are true:
        1. Its trust level ≤ the audience's minimum trust level.
        2. None of the memory's tags intersect the audience's exclusion list.

        Conservative defaults:
        - Empty/missing context_tag → treat as @chef-only (level 4).
        - If audience has no members → block unless it's explicitly @chef-only.

        Args:
            memory_context_tag: The ``@context`` tag of the memory/seed.
            audience: The resolved audience profile for the channel.
            memory_tags: Optional list of free-form memory tags to check
                         against audience exclusions.

        Returns:
            bool: True if the content may be shown to this audience.
        """
        # Determine the content's required trust level
        content_level = tag_to_level(memory_context_tag)

        # Gate 1: trust level check
        # content_level must be ≤ audience.min_trust
        # e.g. @work-circle(2) content in a @public(0) audience → blocked
        if content_level > audience.min_trust:
            return False

        # Gate 2: exclusion check — any overlap with audience exclusions?
        if audience.exclusions and memory_tags:
            for tag in memory_tags:
                if tag in audience.exclusions:
                    return False

        return True
