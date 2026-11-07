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

Conservative default: unknown channel = PUBLIC audience (sees only @public),
unknown tag = CHEF_ONLY (needs the owner to read it).
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
    min_trust: AudienceLevel = AudienceLevel.PUBLIC
    exclusions: set[str] = field(default_factory=set)
    context_tag: str = "@public"


class AudienceResolver:
    """Resolves audience profiles and checks memory access permissions.

    Loads configuration from a JSON file (audience_config.json) and
    provides methods to resolve channel audiences and check whether
    a memory is allowed for a given audience.

    Conservative defaults:
    - Unknown channel, or one with no members → PUBLIC audience (only @public
      content shown; CHEF_ONLY as an AUDIENCE ceiling would show everything)
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

        An unknown channel, or a known one with no members, resolves to a PUBLIC
        audience: the lowest ceiling, so only @public content is shown. (The old
        CHEF_ONLY fallback was the HIGHEST ceiling and blocked nothing.)

        Args:
            channel_id: The channel identifier.

        Returns:
            AudienceProfile: Resolved profile.
        """
        channels = self._config.get("channels", {})
        chan = channels.get(channel_id)

        if chan is None:
            # Unknown channel → maximum restriction = the LOWEST ceiling
            logger.debug("Unknown channel '%s', defaulting to PUBLIC", channel_id)
            return AudienceProfile(
                channel_id=channel_id,
                name="[unknown]",
                members=[],
                min_trust=AudienceLevel.PUBLIC,
                exclusions=set(),
                context_tag="@public",
            )

        members: list[str] = chan.get("members", [])
        context_tag: str = chan.get("context_tag", "@chef-only")

        # Compute effective trust = MIN(member trust levels)
        # If no members are listed, nobody vouches for the room: public only
        if not members:
            min_trust = AudienceLevel.PUBLIC
            context_tag = "@public"
            exclusions: set[str] = set()
        else:
            trust_levels: list[AudienceLevel] = []
            all_exclusions: set[str] = set()
            for member_name in members:
                person = self._get_person(member_name)
                trust_levels.append(self.get_person_trust(member_name))
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
        person = self._config.get("people", {}).get(name, {})
        return person if isinstance(person, dict) else {}

    def get_person_trust(self, name: str) -> AudienceLevel:
        """Get the trust level for a named person (fails closed).

        This is the ONE place a person's level is decided; ``resolve_audience``
        goes through it too. An unknown person, a known person with no
        ``trust_level``, and a malformed ``trust_level`` (not an integer 0 to 4,
        booleans included) all resolve to PUBLIC, the lowest ceiling, and log a
        warning. A level is never invented upward.

        Args:
            name: Person's name as it appears in audience_config.json.

        Returns:
            AudienceLevel: The trust level for this person.
        """
        person = self._get_person(name)
        if not person:
            logger.warning("Unknown person '%s', defaulting to PUBLIC trust", name)
            return AudienceLevel.PUBLIC
        if "trust_level" not in person:
            logger.warning("Person '%s' has no trust_level, defaulting to PUBLIC trust", name)
            return AudienceLevel.PUBLIC
        raw = person["trust_level"]
        if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 4:
            return AudienceLevel(raw)
        logger.warning(
            "Person '%s' has malformed trust_level %r, defaulting to PUBLIC trust", name, raw
        )
        return AudienceLevel.PUBLIC

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


# ── Reader scope (search-path enforcement) ───────────────────────────────────
#
# ``AudienceResolver`` decides what a CHANNEL may see (used by the ritual).
# ``ReaderScope`` is the same decision expressed for the search path: the exact
# set of ``context_tag`` values a reader may receive, which every retrieval arm
# can filter on, plus the never-share tag exclusions applied after hydration.
#
# Reader grammar (the string a caller declares):
#
#   None                     undeclared. The OWNER, unless SKMEMORY_READER is set.
#   @chef / @chef-only /     the owner. No filter.
#   owner
#   @public, @community,     a trust level: sees context tags at or below it.
#   @work-circle,
#   @inner-circle
#   @work:<x> / @inner:<x>   the level below it plus its own scoped tag only
#                            (client isolation: @work:a never sees @work:b).
#   channel:<id>             a configured channel: its members' MIN trust, its
#                            own context_tag, and its members' never_share tags.
#                            An unknown channel or one with no members is
#                            @public (the ritual resolver agrees since SKMEM-P0-20;
#                            as an audience ceiling @chef-only meant "sees everything").
#   anything else            @public, with a warning. A declared reader that
#                            cannot be parsed never widens access.
#
# SKMEMORY_READER declares the process-wide reader and is a CEILING: a per-call
# reader can only narrow it, never widen it, so a prompt-injected tool argument
# cannot escalate a non-owner process.

READER_ENV = "SKMEMORY_READER"

_OWNER_READERS = frozenset({"@chef", "@chef-only", "owner"})


@dataclass(frozen=True)
class ReaderScope:
    """What a declared reader may receive from search.

    Attributes:
        allowed_tags: Exact ``context_tag`` values permitted, or ``None`` for
            unrestricted (the owner).
        exclusions: Memory tags that must never reach this reader.
        label: The reader as declared, for logs.
    """

    allowed_tags: frozenset[str] | None = None
    exclusions: frozenset[str] = frozenset()
    label: str = "owner"

    @property
    def unrestricted(self) -> bool:
        return self.allowed_tags is None and not self.exclusions

    def narrow(self, other: ReaderScope) -> ReaderScope:
        """The intersection of two scopes (most restrictive of each dimension)."""
        if self.allowed_tags is None:
            allowed = other.allowed_tags
        elif other.allowed_tags is None:
            allowed = self.allowed_tags
        else:
            allowed = self.allowed_tags & other.allowed_tags
        return ReaderScope(
            allowed_tags=allowed,
            exclusions=self.exclusions | other.exclusions,
            label=f"{self.label}&{other.label}",
        )

    def allows(self, context_tag: str | None, tags: list[str] | None = None) -> bool:
        """Whether a memory with this context tag and tags may reach the reader."""
        if self.allowed_tags is not None and (context_tag or "") not in self.allowed_tags:
            return False
        return not (self.exclusions and any(t in self.exclusions for t in (tags or [])))


def _tags_up_to(level: AudienceLevel) -> set[str]:
    return {tag for tag, lvl in _TAG_TO_LEVEL.items() if lvl <= level}


def resolve_reader(reader: str | None, resolver: AudienceResolver | None = None) -> ReaderScope:
    """Turn a declared reader into a :class:`ReaderScope`.

    Combines the per-call *reader* with the ``SKMEMORY_READER`` environment
    ceiling. Both absent means the owner (today's behaviour for local stdio MCP
    and CLI sessions).
    """
    import os

    env = (os.environ.get(READER_ENV) or "").strip()
    scope = _scope_for(reader, resolver) if reader is not None else ReaderScope()
    if env:
        scope = _scope_for(env, resolver).narrow(scope)
    return scope


def _scope_for(reader: str, resolver: AudienceResolver | None) -> ReaderScope:
    if not isinstance(reader, str):
        logger.warning("Non-string reader %r, treating as @public", type(reader).__name__)
        return ReaderScope(frozenset(_tags_up_to(AudienceLevel.PUBLIC)), label="@public(unparsed)")
    name = reader.strip()
    low = name.casefold()
    if low in _OWNER_READERS:
        return ReaderScope(label=low)
    if low in _TAG_TO_LEVEL:
        return ReaderScope(frozenset(_tags_up_to(AudienceLevel(_TAG_TO_LEVEL[low]))), label=low)
    for prefix, base in (
        ("@work:", AudienceLevel.WORK_CIRCLE),
        ("@inner:", AudienceLevel.INNER_CIRCLE),
    ):
        if low.startswith(prefix) and len(low) > len(prefix):
            below = AudienceLevel(base - 1)
            return ReaderScope(frozenset(_tags_up_to(below) | {low}), label=low)
    if low.startswith("channel:") and len(low) > len("channel:"):
        # channel ids are case-sensitive in the config; use the declared spelling
        channel_id = name.split(":", 1)[1]
        res = resolver or AudienceResolver()
        known = channel_id in res._config.get("channels", {})
        profile = res.resolve_audience(channel_id)
        if not known or not profile.members:
            return ReaderScope(frozenset(_tags_up_to(AudienceLevel.PUBLIC)), label=low)
        allowed = _tags_up_to(profile.min_trust)
        if profile.context_tag and tag_to_level(profile.context_tag) <= profile.min_trust:
            allowed.add(profile.context_tag)
        return ReaderScope(frozenset(allowed), frozenset(profile.exclusions), label=low)
    logger.warning("Unrecognised reader %r, treating as @public", reader)
    return ReaderScope(frozenset(_tags_up_to(AudienceLevel.PUBLIC)), label="@public(unparsed)")
