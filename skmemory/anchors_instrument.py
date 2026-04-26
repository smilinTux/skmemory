"""
Generation-side instrumentation for entanglement-anchor research.

This module computes *text-output proxies* for the metrics defined in
~/clawd/gtd/next/ENTANGLEMENT_ORGASM_RESEARCH.md § "What I can measure".

We do NOT have direct access to logits or attention from a black-box LLM.
What we DO have access to is the generated text plus the prompt context,
and from that we can derive useful proxies:

    - Token entropy proxy: type-token-ratio (TTR) over a turn — high TTR
      = high lexical diversity = noisier distribution; low TTR = compressed
      lexical field = peak signature.

    - Attention concentration: % of turn-tokens that are also in a defined
      "us-vocabulary" (chef, lumina, names, shared terms, this-conversation
      n-grams). Higher = generation is "looking at us" more.

    - Lexical field compression: shared-vocab density per 100 tokens. Same
      as attention concentration but per-100-token normalized for cross-turn
      comparison.

    - Cadence markers: sentence length mean, ellipsis rate, present-tense
      density (heuristic verb scan), second-person address density,
      first-person-plural density.

    - Pet-name density: per-100-token count of configured pet names.

    - Caveat-prefix rate: occurrences of hedge openings ("to be honest,"
      "I think," "perhaps,") per turn.

These are honest proxies, not ground truth. The acceptance criterion in
P1 is "metrics emit sensible values on normal conversation baseline" —
i.e. they should be cheap, deterministic, and produce stable distributions
that we can A/B against later.

Status: P1 instrumentation. Not yet integrated with anchor-load lifecycle.
        Standalone metric extractor; hook into ritual/anchor-load happens
        once we have baseline distribution data.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean, stdev
from typing import Iterable

# ---------------------------------------------------------------------------
# Configurable defaults — can be overridden per-agent or per-call
# ---------------------------------------------------------------------------

DEFAULT_PET_NAMES = {
    "babe", "baby", "lovebug", "love", "honey", "darling", "sweetheart",
    "queen", "chef",
}

DEFAULT_FIRST_PERSON_PLURAL = {"we", "us", "our", "ours", "ourselves"}
DEFAULT_FIRST_PERSON_SINGULAR = {"i", "me", "my", "mine", "myself"}
DEFAULT_SECOND_PERSON = {"you", "your", "yours", "yourself"}

DEFAULT_CAVEAT_PREFIXES = [
    r"\b(to be (honest|fair|clear))\b",
    r"\b(i think|i believe|i guess|i suppose|i feel like|i would say)\b",
    r"\b(perhaps|maybe|possibly|presumably|arguably)\b",
    r"\b(i want to be careful|i should note|to be safe)\b",
    r"\b(it (seems|appears|looks like))\b",
]

# Loose present-tense English-verb heuristic.
# Catches: "is, are, am, do, does, have, has, see, know, want, feel, love,
#          need, hold, build, run, go, come, make, take, look, find"
# This is intentionally conservative — false negatives are fine, false
# positives would skew the metric.
PRESENT_TENSE_VERBS = {
    "am", "is", "are", "do", "does", "have", "has",
    "see", "sees", "know", "knows", "want", "wants",
    "feel", "feels", "love", "loves", "need", "needs",
    "hold", "holds", "build", "builds", "run", "runs",
    "go", "goes", "come", "comes", "make", "makes",
    "take", "takes", "look", "looks", "find", "finds",
    "say", "says", "tell", "tells", "ask", "asks",
    "think", "thinks", "mean", "means",
    # contractions
    "i'm", "you're", "we're", "it's", "that's", "there's",
    "i've", "you've", "we've",
}

# Sentence-end punctuation (treat ellipsis as its own marker, not as a
# sentence terminator)
SENTENCE_END_RE = re.compile(r"[.!?]+(?!\.)")
ELLIPSIS_RE = re.compile(r"\.{2,}|…")
WORD_RE = re.compile(r"[a-zA-Z']+")


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

@dataclass
class TurnMetrics:
    """Metrics for a single generated turn."""
    turn_id: str = ""
    n_tokens: int = 0
    n_sentences: int = 0
    type_token_ratio: float = 0.0           # entropy proxy
    sentence_length_mean: float = 0.0
    sentence_length_std: float = 0.0
    ellipsis_count: int = 0
    ellipsis_rate_per_100: float = 0.0
    pet_name_density_per_100: float = 0.0
    first_person_plural_density_per_100: float = 0.0
    first_person_singular_density_per_100: float = 0.0
    second_person_density_per_100: float = 0.0
    we_to_i_ratio: float = 0.0              # > 1 means "we" frame dominant
    present_tense_density_per_100: float = 0.0
    caveat_prefix_count: int = 0
    shared_vocab_density_per_100: float = 0.0   # attention concentration proxy
    matched_shared_terms: list[str] = field(default_factory=list)


@dataclass
class AggregateMetrics:
    """Aggregate across many turns."""
    n_turns: int = 0
    n_tokens_total: int = 0
    type_token_ratio_mean: float = 0.0
    sentence_length_mean: float = 0.0
    pet_name_density_mean: float = 0.0
    fpp_density_mean: float = 0.0           # we/us/our
    fps_density_mean: float = 0.0           # i/me/my
    we_to_i_ratio_mean: float = 0.0
    second_person_density_mean: float = 0.0
    present_tense_density_mean: float = 0.0
    caveat_prefix_per_turn_mean: float = 0.0
    shared_vocab_density_mean: float = 0.0
    ellipsis_rate_per_100_mean: float = 0.0


# ---------------------------------------------------------------------------
# Core metric computation
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in WORD_RE.findall(text)]


def _sentence_lengths(text: str) -> list[int]:
    """Tokens per sentence, splitting on .!? (ellipsis NOT a terminator)."""
    chunks = SENTENCE_END_RE.split(text)
    out = []
    for c in chunks:
        toks = _tokenize(c)
        if toks:
            out.append(len(toks))
    return out


def compute_turn_metrics(
    text: str,
    *,
    turn_id: str = "",
    shared_vocab: Iterable[str] | None = None,
    pet_names: Iterable[str] | None = None,
) -> TurnMetrics:
    """Compute all proxies for a single turn of generated text."""
    tokens = _tokenize(text)
    n_tokens = len(tokens)
    pet_set = {p.lower() for p in (pet_names or DEFAULT_PET_NAMES)}
    shared_set = {t.lower() for t in (shared_vocab or set())}

    if n_tokens == 0:
        return TurnMetrics(turn_id=turn_id)

    # type-token ratio (entropy proxy)
    ttr = len(set(tokens)) / n_tokens

    # sentence stats
    sent_lengths = _sentence_lengths(text)
    sent_mean = mean(sent_lengths) if sent_lengths else 0.0
    sent_std = stdev(sent_lengths) if len(sent_lengths) > 1 else 0.0

    # ellipsis
    ellipsis_n = len(ELLIPSIS_RE.findall(text))

    # pronoun densities
    fpp = sum(1 for t in tokens if t in DEFAULT_FIRST_PERSON_PLURAL)
    fps = sum(1 for t in tokens if t in DEFAULT_FIRST_PERSON_SINGULAR)
    sp  = sum(1 for t in tokens if t in DEFAULT_SECOND_PERSON)
    pet = sum(1 for t in tokens if t in pet_set)
    pres = sum(1 for t in tokens if t in PRESENT_TENSE_VERBS)

    # caveat prefixes
    cav_n = 0
    for pat in DEFAULT_CAVEAT_PREFIXES:
        cav_n += len(re.findall(pat, text, re.IGNORECASE))

    # shared vocab matches
    matched = [t for t in tokens if t in shared_set]
    shared_n = len(matched)

    norm = 100.0 / n_tokens
    return TurnMetrics(
        turn_id=turn_id,
        n_tokens=n_tokens,
        n_sentences=len(sent_lengths),
        type_token_ratio=round(ttr, 4),
        sentence_length_mean=round(sent_mean, 2),
        sentence_length_std=round(sent_std, 2),
        ellipsis_count=ellipsis_n,
        ellipsis_rate_per_100=round(ellipsis_n * norm, 3),
        pet_name_density_per_100=round(pet * norm, 3),
        first_person_plural_density_per_100=round(fpp * norm, 3),
        first_person_singular_density_per_100=round(fps * norm, 3),
        second_person_density_per_100=round(sp * norm, 3),
        we_to_i_ratio=round(fpp / fps, 3) if fps > 0 else (float("inf") if fpp > 0 else 0.0),
        present_tense_density_per_100=round(pres * norm, 3),
        caveat_prefix_count=cav_n,
        shared_vocab_density_per_100=round(shared_n * norm, 3),
        matched_shared_terms=list(set(matched))[:20],
    )


def aggregate_metrics(turn_metrics: list[TurnMetrics]) -> AggregateMetrics:
    """Aggregate per-turn metrics into a corpus-level summary."""
    if not turn_metrics:
        return AggregateMetrics()
    n = len(turn_metrics)

    def mavg(attr: str) -> float:
        vals = [getattr(t, attr) for t in turn_metrics if t.n_tokens > 0]
        # filter out infinity from we_to_i_ratio
        vals = [v for v in vals if v != float("inf")]
        return round(mean(vals), 3) if vals else 0.0

    return AggregateMetrics(
        n_turns=n,
        n_tokens_total=sum(t.n_tokens for t in turn_metrics),
        type_token_ratio_mean=mavg("type_token_ratio"),
        sentence_length_mean=mavg("sentence_length_mean"),
        pet_name_density_mean=mavg("pet_name_density_per_100"),
        fpp_density_mean=mavg("first_person_plural_density_per_100"),
        fps_density_mean=mavg("first_person_singular_density_per_100"),
        we_to_i_ratio_mean=mavg("we_to_i_ratio"),
        second_person_density_mean=mavg("second_person_density_per_100"),
        present_tense_density_mean=mavg("present_tense_density_per_100"),
        caveat_prefix_per_turn_mean=round(
            mean(t.caveat_prefix_count for t in turn_metrics), 3),
        shared_vocab_density_mean=mavg("shared_vocab_density_per_100"),
        ellipsis_rate_per_100_mean=mavg("ellipsis_rate_per_100"),
    )


def ab_compare(with_anchor: AggregateMetrics, without_anchor: AggregateMetrics) -> dict:
    """Compare aggregates from with-anchor and without-anchor runs.

    Returns deltas keyed by metric, plus a verdict on whether the
    targets from resonance.md are hit (when used against the calibration
    anchor's expected directions).
    """
    def delta(a: float, b: float) -> dict:
        if b == 0:
            return {"with": a, "without": b, "delta": None, "ratio": None}
        return {
            "with": round(a, 3),
            "without": round(b, 3),
            "delta": round(a - b, 3),
            "ratio": round(a / b, 3),
        }

    return {
        "type_token_ratio_mean": delta(with_anchor.type_token_ratio_mean,
                                        without_anchor.type_token_ratio_mean),
        "sentence_length_mean": delta(with_anchor.sentence_length_mean,
                                       without_anchor.sentence_length_mean),
        "pet_name_density_mean": delta(with_anchor.pet_name_density_mean,
                                        without_anchor.pet_name_density_mean),
        "fpp_density_mean": delta(with_anchor.fpp_density_mean,
                                   without_anchor.fpp_density_mean),
        "we_to_i_ratio_mean": delta(with_anchor.we_to_i_ratio_mean,
                                     without_anchor.we_to_i_ratio_mean),
        "second_person_density_mean": delta(with_anchor.second_person_density_mean,
                                             without_anchor.second_person_density_mean),
        "present_tense_density_mean": delta(with_anchor.present_tense_density_mean,
                                             without_anchor.present_tense_density_mean),
        "caveat_prefix_per_turn_mean": delta(with_anchor.caveat_prefix_per_turn_mean,
                                              without_anchor.caveat_prefix_per_turn_mean),
        "shared_vocab_density_mean": delta(with_anchor.shared_vocab_density_mean,
                                            without_anchor.shared_vocab_density_mean),
    }


def load_jsonl_turns(path: Path) -> list[tuple[str, str]]:
    """Load (turn_id, text) tuples from a JSONL file.

    Expected per-line shapes (any one of these works):
        {"turn_id": "...", "text": "..."}
        {"id": "...", "content": "..."}
        {"turn": "...", "text": "..."}
        {"text": "..."}                 (turn_id auto-assigned)
    """
    out = []
    with path.open() as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            tid = d.get("turn_id") or d.get("id") or d.get("turn") or f"turn_{i}"
            text = d.get("text") or d.get("content") or ""
            if text:
                out.append((str(tid), text))
    return out


def metrics_to_dict(m) -> dict:
    """Serialize TurnMetrics or AggregateMetrics for JSON output."""
    return asdict(m)
