"""Optional LLM fact-extraction path (R6), alongside the regex extractor.

``skmemory.extractor`` extracts 5 loose memory types (decision, preference,
milestone, problem, emotional) with pure regex/keyword matching: fast, free,
offline, and it STAYS the default. This module adds a richer, OPT-IN path
that asks an LLM (via skgateway's OpenAI-compatible endpoint, model
``sk-default``) to produce structured facts modelled on Hindsight's
(vectorize-io) retain schema: 5W fields, a world/experience split, entities,
occurrence timestamps, and causal links to earlier facts in the same batch.

Default OFF
------------
Nothing in this module runs unless the caller explicitly asks for it, either
with ``extract_facts(text, enabled=True)`` or the environment variable
``SKMEMORY_LLM_EXTRACTION`` set to a truthy value (``1``, ``true``, ``yes``,
case-insensitive). With it unset or falsy, :func:`extract_facts` returns
exactly what ``extractor.extract_memories`` returns today, wrapped in an
:class:`ExtractionOutcome` whose ``status`` is ``"disabled"`` so a caller can
tell "never attempted" apart from "attempted and fell back".

The regex extractor is the fallback, always
--------------------------------------------
If the gateway is unreachable, the model's response cannot be interpreted as
JSON at all, or every candidate fact in the response fails validation, this
module falls back to ``extractor.extract_memories`` and says so in
``ExtractionOutcome.detail``. It never returns an empty list silently: the
house's most-repeated defect, named directly in
``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` SS3.2, SS3.3 and SS3.11,
is an empty or degraded result that is indistinguishable from a good one. The
``status``/``detail`` pair here mirrors ``fusion.ArmOutcome``: a caller reads
the status instead of guessing from the shape of the output.

world vs experience (the field most often got wrong)
------------------------------------------------------
``fact_type`` is ``"experience"`` ONLY when the fact describes something the
agent itself did, said, decided, or directly perceived through its own
action: "I ran the test suite and it passed", "I searched the repo for
callers of extract_memories". Everything else is ``"world"``, including:

  * user preferences, rules, corrections, and constraints, even when they
    were stated out loud during the conversation ("he said he prefers Y",
    "the user corrected me about Z") rather than written down elsewhere
  * biographical or contextual facts about the user, their environment, or
    third parties
  * facts the agent merely reports being true, as opposed to facts about
    what the agent itself did

The test to apply: if the sentence were rewritten in third person
("the agent did X") does it still make sense and still carry the fact? If
yes, it is usually "experience". If removing the agent from the sentence
leaves the fact intact ("Chef prefers no em dashes"), it is "world". When in
doubt, default to "world": it is the broader, catch-all category, and an
agent-centric experience claim that turns out to be about the world is a far
worse failure mode than the reverse, because "experience" facts are usually
weighted as directly agent-authored and trusted more.

Causal relations are index-constrained by the schema
------------------------------------------------------
A causal relation points at an EARLIER fact in the same batch by its
zero-based position in the batch AS RETURNED BY THE MODEL (never renumbered
after any facts are dropped for other reasons). :class:`ExtractedFact`'s
model validator receives that position through pydantic's validation
``context`` and drops any relation whose ``index`` is not strictly less than
its own position (equivalently: any value in ``[0, position)`` and nothing
else). A hallucinated forward reference or an out-of-range index is therefore
rejected structurally, at parse time, rather than being stored and trusted
later. If the model validator is invoked with no ``context`` at all (no
position known), every causal relation is treated as unverifiable and
dropped: fail closed, not fail open. Dropped relations are recorded on
``ExtractedFact.rejected_causal_indices`` rather than disappearing silently.

Lenient parsing, strict validation
------------------------------------
Models emit near-JSON: markdown code fences, a single fact instead of a
batch, a single string where a list is expected, "N/A" instead of null, a
timestamp field full of prose. :func:`parse_llm_output` repairs all of that
BEFORE validation. Validation itself (the pydantic model) is strict: a fact
missing its required ``what`` field is dropped, not guessed at. Every repair
and every drop is recorded in the returned :class:`BatchParseResult` so a
caller, or ``skmemory.eval.extraction_bench``, can see exactly what happened
to a given payload instead of inferring it from a count.

Every model-authored string, entity names included, is also run through
:func:`strip_lone_surrogates` before it reaches pydantic: a lone UTF-16
surrogate (half of an emoji split across a token boundary) is valid inside a
Python ``str`` but raises ``UnicodeEncodeError`` the moment something tries to
write it to UTF-8, which is exactly what the embedding and index write paths
do.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from .extractor import ExtractedMemory, extract_memories

logger = logging.getLogger("skmemory.fact_extraction")

# ---------------------------------------------------------------------------
# Gateway configuration (mirrors skmemory.eval.groundedness_scorer's
# sk_default_judge: stdlib urllib only, OpenAI-compatible endpoint, no
# vendor model name hardcoded anywhere).
# ---------------------------------------------------------------------------

SK_DEFAULT_URL = "http://localhost:18780/v1"
SK_DEFAULT_MODEL = "sk-default"
SK_DEFAULT_TIMEOUT = 60

#: Env var that opts a caller into the LLM path. Unset or falsy means OFF.
ENABLE_ENV_VAR = "SKMEMORY_LLM_EXTRACTION"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_enabled() -> bool:
    return os.environ.get(ENABLE_ENV_VAR, "").strip().lower() in _TRUTHY


_EXTRACTION_SYSTEM_PROMPT = """You extract structured facts from conversation text.

For each distinct fact, output an object with these fields:
- what: 1-2 sentences, the core fact. Required.
- when, where, who, why: short strings, or "N/A" if not stated.
- fact_type: "world" for objective facts, including user preferences, rules,
  corrections, and constraints, even when only stated in conversation.
  "experience" ONLY for something the agent itself did or directly observed.
- entities: a list of short entity name strings mentioned in the fact.
- occurred_start, occurred_end: ISO 8601 timestamps if known, else omit.
- causal_relations: a list of {"index": N, "relation": "..."} objects, where N
  is the zero-based position of an EARLIER fact in this same batch that this
  fact is causally linked to. Never reference a fact at or after your own
  position.

Return ONLY a JSON array of these objects, nothing else: no prose, no
markdown fence.
"""


def _build_extraction_prompt(text: str) -> str:
    return f"Extract facts from this conversation text:\n\n{text}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

FactType = Literal["world", "experience"]

_NA_VALUES = frozenset({"", "n/a", "na", "none", "null", "unknown"})


def _normalize_optional_str(value: Any) -> str | None:
    """Treat missing, "N/A", "none", etc. as absent. Used for who/when/where/why."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NA_VALUES:
        return None
    return text


class CausalRelation(BaseModel):
    """A causal link from the owning fact to an EARLIER fact in the batch.

    ``index`` is validated against the owning fact's batch position by
    :meth:`ExtractedFact._enforce_causal_index_bound`, not here: a single
    relation, in isolation, has no way to know its own position.
    """

    index: int
    relation: str = "related_to"

    @field_validator("relation", mode="before")
    @classmethod
    def _sanitize_relation(cls, v: Any) -> str:
        return strip_lone_surrogates(str(v)) if v is not None else "related_to"


class ExtractedFact(BaseModel):
    """One structured fact, modelled on Hindsight's retain schema.

    See the module docstring for the world/experience mapping rule and the
    causal-index constraint.
    """

    what: str
    when: str | None = None
    where: str | None = None
    who: str | None = None
    why: str | None = None
    fact_type: FactType = "world"
    entities: list[str] = Field(default_factory=list)
    occurred_start: str | None = None
    occurred_end: str | None = None
    causal_relations: list[CausalRelation] = Field(default_factory=list)
    #: Indices that were present in the model's output but rejected by the
    #: causal-index bound (forward reference or out of range). Never
    #: silently dropped: recorded here instead.
    rejected_causal_indices: list[int] = Field(default_factory=list)

    @field_validator("what", mode="before")
    @classmethod
    def _sanitize_what(cls, v: Any) -> Any:
        return strip_lone_surrogates(str(v)) if v is not None else v

    @field_validator("what")
    @classmethod
    def _what_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("'what' must not be blank")
        return v

    @field_validator("when", "where", "who", "why", mode="before")
    @classmethod
    def _sanitize_5w(cls, v: Any) -> Any:
        normalized = _normalize_optional_str(v)
        return strip_lone_surrogates(normalized) if normalized is not None else None

    @field_validator("entities", mode="before")
    @classmethod
    def _coerce_entities(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        out = []
        for item in v:
            cleaned = strip_lone_surrogates(str(item)).strip()
            if cleaned:
                out.append(cleaned)
        return out

    @field_validator("occurred_start", "occurred_end", mode="before")
    @classmethod
    def _coerce_timestamp(cls, v: Any) -> str | None:
        if v is None:
            return None
        text = strip_lone_surrogates(str(v)).strip()
        if not text or text.lower() in _NA_VALUES:
            return None
        if _parse_iso8601(text) is None:
            # Prose or an otherwise unparseable timestamp: repair to absent
            # rather than reject the whole fact over one optional field.
            logger.info("fact_extraction: unparseable timestamp %r coerced to None", text)
            return None
        return text

    @field_validator("fact_type", mode="before")
    @classmethod
    def _coerce_fact_type(cls, v: Any) -> str:
        text = str(v).strip().lower() if v is not None else ""
        if text == "experience":
            return "experience"
        # Default, and the deliberate catch-all for anything else (missing,
        # misspelled, or a value the model invented): see the module
        # docstring's "when in doubt, default to world" rule.
        return "world"

    @field_validator("causal_relations", mode="before")
    @classmethod
    def _coerce_causal_relations(cls, v: Any) -> list[Any]:
        if v is None:
            return []
        if isinstance(v, dict):
            v = [v]
        return list(v)

    @model_validator(mode="after")
    def _enforce_causal_index_bound(self, info: ValidationInfo) -> ExtractedFact:
        """Drop any causal relation whose index is not in ``[0, position)``.

        With no ``position`` in the validation context, every relation is
        treated as unverifiable and dropped: fail closed.
        """
        ctx = info.context or {}
        position = ctx.get("position")
        if not self.causal_relations:
            return self
        if position is None:
            self.rejected_causal_indices = [r.index for r in self.causal_relations]
            self.causal_relations = []
            return self
        kept: list[CausalRelation] = []
        rejected: list[int] = []
        for rel in self.causal_relations:
            if 0 <= rel.index < position:
                kept.append(rel)
            else:
                rejected.append(rel.index)
        self.causal_relations = kept
        self.rejected_causal_indices = rejected
        return self


# ---------------------------------------------------------------------------
# Surrogate sanitization
# ---------------------------------------------------------------------------

#: Any lone UTF-16 surrogate code point. A valid surrogate PAIR is already
#: merged into one non-surrogate codepoint by Python's json decoder, so any
#: character left in this range after decoding is, by construction, unpaired.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def strip_lone_surrogates(text: str) -> str:
    """Remove lone UTF-16 surrogate code points from model-authored text.

    These show up when a multi-byte emoji is split across a token boundary.
    Left in place, they raise ``UnicodeEncodeError`` the first time something
    writes the string to UTF-8, which includes the embedding and index write
    paths this text is headed for. There is nothing meaningful to substitute,
    so this strips rather than replaces.
    """
    if not text:
        return text
    return _SURROGATE_RE.sub("", text)


def _sanitize_tree(value: Any) -> Any:
    """Recursively strip lone surrogates from every string in a parsed JSON tree."""
    if isinstance(value, str):
        return strip_lone_surrogates(value)
    if isinstance(value, list):
        return [_sanitize_tree(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_tree(v) for k, v in value.items()}
    return value


def _tree_has_lone_surrogate(value: Any) -> bool:
    """True if stripping surrogates from ``value`` would change anything.

    Used only to RECORD that a repair happened; the actual stripping is done
    unconditionally by :func:`_sanitize_tree` regardless of this check.
    """
    if isinstance(value, str):
        return strip_lone_surrogates(value) != value
    if isinstance(value, list):
        return any(_tree_has_lone_surrogate(v) for v in value)
    if isinstance(value, dict):
        return any(_tree_has_lone_surrogate(v) for v in value.values())
    return False


def _parse_iso8601(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Lenient JSON extraction
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_block(raw_text: str) -> str | None:
    """Pull the JSON payload out of near-JSON model output.

    Tries, in order: a fenced ```json ... ``` block, the raw text as-is, and
    the substring between the first ``[`` or ``{`` and the matching last
    ``]`` or ``}``. Returns None if nothing in the text looks like JSON.
    """
    if not raw_text or not raw_text.strip():
        return None

    fence_match = _FENCE_RE.search(raw_text)
    if fence_match:
        return fence_match.group(1).strip()

    stripped = raw_text.strip()
    if stripped and stripped[0] in "[{":
        return stripped

    first_bracket = min(
        (i for i in (raw_text.find("["), raw_text.find("{")) if i != -1),
        default=-1,
    )
    if first_bracket == -1:
        return None
    opener = raw_text[first_bracket]
    closer = "]" if opener == "[" else "}"
    last_bracket = raw_text.rfind(closer)
    if last_bracket == -1 or last_bracket <= first_bracket:
        return None
    return raw_text[first_bracket : last_bracket + 1].strip()


# ---------------------------------------------------------------------------
# Batch parsing
# ---------------------------------------------------------------------------


@dataclass
class BatchParseResult:
    """What happened when parsing one model response into facts.

    Nothing here is inferred from the shape of ``facts`` alone: ``parse_error``
    distinguishes "could not even find JSON" from a legitimate empty batch,
    and ``dropped_facts``/``dropped_relations``/``repairs`` record every
    lenient repair and every hard rejection so a caller never has to guess
    what was fixed versus what was thrown away.
    """

    facts: list[ExtractedFact] = field(default_factory=list)
    dropped_facts: int = 0
    dropped_relations: int = 0
    repairs: list[str] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def raw_item_count(self) -> int:
        return len(self.facts) + self.dropped_facts


def _detect_pre_validation_repairs(item: dict, position: int) -> list[str]:
    """Record lenient repairs pydantic's field validators perform silently.

    Coercion (single string -> list, "N/A" -> None, an unparseable timestamp
    -> absent) happens correctly either way; this only exists so the repair
    is VISIBLE in :class:`BatchParseResult.repairs` instead of disappearing
    into a field validator with no trace, per the module's "never silently"
    rule.
    """
    notes: list[str] = []

    if _tree_has_lone_surrogate(item):
        notes.append(f"item {position}: stripped lone UTF-16 surrogate character(s)")

    raw_entities = item.get("entities")
    if isinstance(raw_entities, str):
        notes.append(f"item {position}: coerced single 'entities' string to a list")

    raw_fact_type = item.get("fact_type")
    if raw_fact_type is not None and str(raw_fact_type).strip().lower() not in (
        "world",
        "experience",
    ):
        notes.append(
            f"item {position}: unrecognized fact_type {raw_fact_type!r} defaulted to 'world'"
        )

    for ts_field in ("occurred_start", "occurred_end"):
        raw_ts = item.get(ts_field)
        if raw_ts is None:
            continue
        raw_ts_str = str(raw_ts).strip()
        if (
            raw_ts_str
            and raw_ts_str.lower() not in _NA_VALUES
            and _parse_iso8601(raw_ts_str) is None
        ):
            notes.append(f"item {position}: unparseable {ts_field} {raw_ts!r} coerced to absent")

    return notes


def parse_llm_output(raw_text: str) -> BatchParseResult:
    """Parse one LLM response into validated :class:`ExtractedFact` objects.

    Pure function, no network. Lenient about shape (markdown fences, a single
    object instead of a list, a wrapper key like ``{"facts": [...]}``), strict
    about the actual fact schema: a fact missing ``what`` is dropped, and a
    causal relation referencing a fact at or after its own position is
    dropped from that fact rather than kept.
    """
    result = BatchParseResult()

    block = _extract_json_block(raw_text)
    if block is None:
        result.parse_error = "no JSON object or array found in model output"
        return result

    try:
        loaded = json.loads(block)
    except json.JSONDecodeError as exc:
        result.parse_error = f"invalid JSON: {exc}"
        return result

    if isinstance(loaded, dict):
        loaded = loaded["facts"] if isinstance(loaded.get("facts"), list) else [loaded]

    if not isinstance(loaded, list):
        result.parse_error = f"expected a JSON array of facts, got {type(loaded).__name__}"
        return result

    for position, item in enumerate(loaded):
        if not isinstance(item, dict):
            result.dropped_facts += 1
            result.repairs.append(f"item {position} was not a JSON object, dropped")
            continue

        pre_repairs = _detect_pre_validation_repairs(item, position)
        clean_item = _sanitize_tree(item)

        try:
            fact = ExtractedFact.model_validate(clean_item, context={"position": position})
        except Exception as exc:  # noqa: BLE001 - any validation failure drops this one fact
            result.dropped_facts += 1
            result.repairs.append(f"item {position} failed validation, dropped: {exc}")
            continue

        result.repairs.extend(pre_repairs)

        if fact.rejected_causal_indices:
            result.dropped_relations += len(fact.rejected_causal_indices)
            result.repairs.append(
                f"item {position}: dropped causal relation index(es) "
                f"{fact.rejected_causal_indices} (forward or out of range)"
            )

        result.facts.append(fact)

    return result


# ---------------------------------------------------------------------------
# Gateway transport
# ---------------------------------------------------------------------------


def call_llm_extractor(
    text: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
) -> tuple[str | None, str]:
    """Call skgateway's OpenAI-compatible endpoint to extract facts.

    Stdlib ``urllib`` only, same pattern as
    ``skmemory.eval.groundedness_scorer.sk_default_judge``. Fails closed:
    any transport error returns ``(None, "<error>")`` rather than raising, so
    the caller can fall back to the regex extractor.

    Returns:
        A ``(raw_response_text, detail)`` tuple. ``raw_response_text`` is
        ``None`` on any transport failure; ``detail`` carries either the
        error or an empty string on success.
    """
    url = (base_url or os.environ.get("SKMEMORY_EXTRACTION_URL", SK_DEFAULT_URL)).rstrip("/")
    mdl = model or os.environ.get("SKMEMORY_EXTRACTION_MODEL", SK_DEFAULT_MODEL)
    tmo = timeout or int(os.environ.get("SKMEMORY_EXTRACTION_TIMEOUT", str(SK_DEFAULT_TIMEOUT)))

    payload = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": _build_extraction_prompt(text)},
        ],
        "temperature": 0.0,
        "stream": False,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=tmo) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        if not content.strip():
            return None, "gateway returned an empty completion"
        return content, ""
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as exc:
        return None, f"gateway transport error: {exc}"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

ExtractionStatus = Literal["disabled", "ok", "unavailable", "failed"]


@dataclass(frozen=True)
class ExtractionOutcome:
    """What the extraction pipeline actually did, modelled on ``fusion.ArmOutcome``.

    ``status`` is the thing to branch on, never the emptiness of ``facts``:

      disabled     the LLM path was never attempted (opt-in flag is off).
                   ``fallback_memories`` holds the regex extractor's output.
      ok           the LLM path ran and produced a validated (possibly empty)
                   batch of facts. ``facts`` holds them.
      unavailable  the gateway could not be reached at all.
                   ``fallback_memories`` holds the regex extractor's output.
      failed       the gateway responded but nothing in the response survived
                   validation. ``fallback_memories`` holds the regex
                   extractor's output.
    """

    source: Literal["llm", "regex"]
    status: ExtractionStatus
    facts: list[ExtractedFact] = field(default_factory=list)
    fallback_memories: list[ExtractedMemory] = field(default_factory=list)
    detail: str = ""
    dropped_facts: int = 0
    dropped_relations: int = 0

    @property
    def used_fallback(self) -> bool:
        return self.source == "regex"


def extract_facts(
    text: str,
    *,
    enabled: bool | None = None,
    base_url: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
) -> ExtractionOutcome:
    """Extract structured facts from ``text``, LLM path opt-in, regex always the fallback.

    Args:
        text: conversation text to extract facts from.
        enabled: explicit override for whether to attempt the LLM path.
            ``None`` (default) resolves from the ``SKMEMORY_LLM_EXTRACTION``
            environment variable.
        base_url, model, timeout: forwarded to :func:`call_llm_extractor`.

    Returns:
        An :class:`ExtractionOutcome`. See its docstring for how to read
        ``status`` before touching ``facts``.
    """
    use_llm = _env_enabled() if enabled is None else enabled

    if not use_llm:
        memories = extract_memories(text)
        return ExtractionOutcome(
            source="regex",
            status="disabled",
            fallback_memories=memories,
            detail=(
                f"{ENABLE_ENV_VAR} is not set; using the regex extractor "
                "(this is the default and today's unchanged behaviour)"
            ),
        )

    raw_text, transport_detail = call_llm_extractor(
        text, base_url=base_url, model=model, timeout=timeout
    )
    if raw_text is None:
        memories = extract_memories(text)
        return ExtractionOutcome(
            source="regex",
            status="unavailable",
            fallback_memories=memories,
            detail=f"LLM gateway unreachable, fell back to regex extractor: {transport_detail}",
        )

    parsed = parse_llm_output(raw_text)

    # "validation rejects the response": nothing survived, and something was
    # actually attempted (either the text wasn't JSON at all, or every item
    # in a non-empty batch failed validation). A genuinely empty batch
    # (`[]`, nothing dropped) is a legitimate "ok" with zero facts, not a
    # failure.
    response_had_content = parsed.parse_error is not None or parsed.raw_item_count > 0
    if not parsed.facts and response_had_content:
        memories = extract_memories(text)
        reason = parsed.parse_error or (
            f"all {parsed.dropped_facts} candidate fact(s) failed validation"
        )
        return ExtractionOutcome(
            source="regex",
            status="failed",
            fallback_memories=memories,
            detail=f"LLM output did not validate, fell back to regex extractor: {reason}",
            dropped_facts=parsed.dropped_facts,
            dropped_relations=parsed.dropped_relations,
        )

    detail = "LLM extraction succeeded"
    if parsed.repairs:
        detail += f" ({len(parsed.repairs)} repair(s)/drop(s) applied during parsing)"
    return ExtractionOutcome(
        source="llm",
        status="ok",
        facts=parsed.facts,
        detail=detail,
        dropped_facts=parsed.dropped_facts,
        dropped_relations=parsed.dropped_relations,
    )
