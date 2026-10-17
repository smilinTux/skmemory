#!/usr/bin/env python3
"""Offline harness for the optional LLM fact-extraction path (R6).

Why this file exists
---------------------
``skmemory.extractor.extract_memories`` is pure regex over 5 loose types
(decision, preference, milestone, problem, emotional). It is fast, free, and
it never leaves the machine, but its patterns only fire on specific surface
phrasing: ``\\b(?:we|i)\\s+(?:decided|agreed|...)\\b`` catches "we decided"
but not "the team decided", and it has no notion of who/where/why, no
entities, and no causal structure between facts. ``skmemory.fact_extraction``
exists to add that richer structure as an OPT-IN alternative, never a
replacement. Before anyone turns it on, there should be a number for what it
actually buys, and a number for how it behaves on output a real model is
likely to produce, which is messier than clean JSON.

This harness measures two different things, and keeps them separate because
they answer two different questions:

1. Coverage: on the SAME input text, how many facts does the regex extractor
   find versus how many does the LLM path find on a RECORDED model response
   for that text. This is the actual case for adding R6, not an abstract
   claim: the regex extractor structurally cannot see a fact phrased as "the
   team decided" or "the agent traced the bug to X", because nothing in
   ``DECISION_PATTERNS``/``PROBLEM_PATTERNS`` matches third-person or agent-
   framed phrasing. One fixture below (``live_em_dash_pydantic``) is a
   VERBATIM capture from a real call to sk-default via skgateway
   (``http://localhost:18780/v1``, captured 2026-10-17), frozen here so the
   harness stays offline; the rest are hand-authored fixtures representative
   of plausible model output, labelled as such rather than claimed as live
   captures.

2. Parse robustness: a fixed table of malformed payloads (the actual shapes
   ``docs/postmortems/2026-10-16-sqlite-only-orphans.md`` warns about: a
   silent wrong answer that looks like a right one) run through
   ``skmemory.fact_extraction.parse_llm_output`` with no network involved,
   showing whether each is REJECTED (the fact is dropped, nothing stored) or
   REPAIRED (something is coerced or an invalid sub-part is dropped, but the
   fact survives). Every row has a scripted expectation, so this table is
   self-checking the same way ``tests/test_fusion_bench_fixture.py`` is
   self-checking for ``fusion_bench``: if the behaviour drifts, the row's
   ``ok`` column goes False instead of the drift hiding in a changed count.

No network, no randomness: every fixture here is a fixed string. Run with:

    python -m skmemory.eval.extraction_bench            # both tables
    python -m skmemory.eval.extraction_bench --json      # machine-readable
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from skmemory.extractor import extract_memories
from skmemory.fact_extraction import parse_llm_output

# =============================================================================
# PART 1: COVERAGE COMPARISON
# =============================================================================


@dataclass(frozen=True)
class CoverageFixture:
    """One (input text, recorded model response) pair.

    ``source`` says exactly where ``recorded_response`` came from, so nobody
    mistakes a hand-authored example for a live capture or vice versa.
    """

    fixture_id: str
    input_text: str
    recorded_response: str
    source: str


COVERAGE_FIXTURES: list[CoverageFixture] = [
    CoverageFixture(
        fixture_id="live_em_dash_pydantic",
        input_text=(
            "Chef said he never wants em dashes in any output, going forward.\n"
            "The agent then grepped the fact_extraction.py file for em dashes and "
            "confirmed there were none.\n"
            "Later, the team decided to use pydantic for schema validation because "
            "it was already a dependency.\n"
        ),
        # VERBATIM capture: POST http://localhost:18780/v1/chat/completions,
        # model sk-default, temperature 0.0, captured 2026-10-17. Not
        # re-requested by this harness; frozen here for determinism.
        recorded_response="""[
  {
    "what": "Chef stated a preference to never use em dashes in any output going forward.",
    "when": "N/A",
    "where": "N/A",
    "who": "Chef",
    "why": "N/A",
    "fact_type": "world",
    "entities": ["Chef", "em dashes"]
  },
  {
    "what": "The agent grepped the fact_extraction.py file for em dashes and confirmed there were none.",
    "when": "N/A",
    "where": "N/A",
    "who": "agent",
    "why": "N/A",
    "fact_type": "experience",
    "entities": ["agent", "fact_extraction.py", "em dashes"]
  },
  {
    "what": "The team decided to use pydantic for schema validation.",
    "when": "N/A",
    "where": "N/A",
    "who": "team",
    "why": "pydantic was already a dependency",
    "fact_type": "world",
    "entities": ["team", "pydantic", "schema validation"]
  }
]""",
        source="live capture, sk-default via skgateway, 2026-10-17",
    ),
    CoverageFixture(
        fixture_id="synthetic_shipped_and_traced_bug",
        input_text=(
            "We shipped the retrieval fusion work today. All tests pass and the "
            "build succeeded.\n"
            "Earlier in the week we hit a nasty bug where the dense arm returned "
            "empty lists silently, which we eventually traced to a swallowed "
            "exception in the vector backend.\n"
        ),
        # Hand-authored fixture representative of plausible model output.
        # NOT a live capture. Demonstrates a causal_relations link.
        recorded_response="""[
  {
    "what": "The dense retrieval arm returned empty lists silently because of a swallowed exception in the vector backend.",
    "fact_type": "world",
    "entities": ["dense arm", "vector backend"]
  },
  {
    "what": "The agent traced the silent empty-list failure to the swallowed exception and fixed it.",
    "fact_type": "experience",
    "entities": ["agent", "vector backend"],
    "causal_relations": [{"index": 0, "relation": "root_cause_of"}]
  },
  {
    "what": "The team shipped the retrieval fusion work with all tests passing and the build succeeding.",
    "fact_type": "experience",
    "entities": ["retrieval fusion"],
    "causal_relations": [{"index": 1, "relation": "preceded_by"}]
  }
]""",
        source="hand-authored, representative fixture (not a live capture)",
    ),
    CoverageFixture(
        fixture_id="synthetic_third_person_rule_statement",
        input_text=(
            "The operator explained that any migration touching the memories "
            "table has to be idempotent, because docker compose replays the "
            "whole migrations.txt list on every fresh boot.\n"
        ),
        # Hand-authored. The regex extractor's rule-statement patterns require
        # first person ("from now on", "rule:") or direct imperatives; a
        # third-person explanation like this matches none of them.
        recorded_response="""[
  {
    "what": "Any migration touching the memories table must be idempotent.",
    "who": "the operator",
    "why": "docker compose replays the whole migrations.txt list on every fresh boot",
    "fact_type": "world",
    "entities": ["memories table", "migrations.txt", "docker compose"]
  }
]""",
        source="hand-authored, representative fixture (not a live capture)",
    ),
]


@dataclass(frozen=True)
class CoverageRow:
    fixture_id: str
    source: str
    regex_count: int
    llm_count: int
    regex_types: list[str]
    llm_fact_types: list[str]


def run_coverage_benchmark() -> list[CoverageRow]:
    """Compare regex vs LLM-path fact counts on the same input text, per fixture."""
    rows: list[CoverageRow] = []
    for fx in COVERAGE_FIXTURES:
        regex_memories = extract_memories(fx.input_text)
        parsed = parse_llm_output(fx.recorded_response)
        rows.append(
            CoverageRow(
                fixture_id=fx.fixture_id,
                source=fx.source,
                regex_count=len(regex_memories),
                llm_count=len(parsed.facts),
                regex_types=[m.type for m in regex_memories],
                llm_fact_types=[f.fact_type for f in parsed.facts],
            )
        )
    return rows


# =============================================================================
# PART 2: PARSE ROBUSTNESS (REJECTED vs REPAIRED)
# =============================================================================


@dataclass(frozen=True)
class MalformedCase:
    """One malformed payload with a scripted expectation.

    ``expected`` is ``"rejected"`` when the fact (there is exactly one fact in
    the batch, at position 0 or 1 as noted) must not survive at all, and
    ``"repaired"`` when the fact survives but something inside it was coerced
    or dropped.
    """

    label: str
    payload: list[dict]
    expected: str
    why: str


MALFORMED_CASES: list[MalformedCase] = [
    MalformedCase(
        label="missing field (no 'what')",
        payload=[{"when": "today", "who": "Chef"}],
        expected="rejected",
        why="'what' is the only required field; nothing to store without it",
    ),
    MalformedCase(
        label="wrong type (entities as a single string)",
        payload=[{"what": "Chef prefers short replies.", "entities": "Chef"}],
        expected="repaired",
        why="a bare string is coerced to a one-element list",
    ),
    MalformedCase(
        label="forward causal index (points at itself)",
        payload=[
            {"what": "first fact"},
            {"what": "second fact", "causal_relations": [{"index": 1}]},
        ],
        expected="repaired",
        why="index 1 at position 1 is a self-reference; the relation is dropped, the fact stays",
    ),
    MalformedCase(
        label="out-of-range causal index",
        payload=[
            {"what": "first fact"},
            {"what": "second fact", "causal_relations": [{"index": 97}]},
        ],
        expected="repaired",
        why="index 97 is nowhere near the batch; the relation is dropped, the fact stays",
    ),
    MalformedCase(
        label="lone UTF-16 surrogate in 'what'",
        payload=[{"what": "fact with \ud83d broken emoji"}],
        expected="repaired",
        why="the unpaired surrogate is stripped so the string can still be UTF-8 encoded",
    ),
    MalformedCase(
        label="prose in a timestamp field",
        payload=[
            {
                "what": "fact with a vague date",
                "occurred_start": "sometime last week, maybe Tuesday",
            }
        ],
        expected="repaired",
        why="unparseable as ISO 8601, so the timestamp is coerced to absent rather than rejecting the fact",
    ),
]


@dataclass(frozen=True)
class MalformedRow:
    label: str
    expected: str
    actual: str
    ok: bool
    detail: str


def _classify(payload: list[dict]) -> tuple[str, str]:
    """Run one malformed payload and classify what actually happened."""
    result = parse_llm_output(json.dumps(payload))
    if not result.facts:
        return "rejected", f"dropped_facts={result.dropped_facts}; {result.repairs}"
    if result.repairs or result.dropped_relations:
        return "repaired", f"repairs={result.repairs}"
    return "accepted", "no repair or rejection occurred"


def run_malformed_payload_benchmark() -> list[MalformedRow]:
    rows: list[MalformedRow] = []
    for case in MALFORMED_CASES:
        actual, detail = _classify(case.payload)
        rows.append(
            MalformedRow(
                label=case.label,
                expected=case.expected,
                actual=actual,
                ok=(actual == case.expected),
                detail=detail,
            )
        )
    return rows


# =============================================================================
# REPORTING
# =============================================================================


def run_benchmark() -> dict:
    coverage = run_coverage_benchmark()
    malformed = run_malformed_payload_benchmark()
    return {
        "coverage": [asdict(r) for r in coverage],
        "malformed_payloads": [asdict(r) for r in malformed],
        "malformed_all_ok": all(r.ok for r in malformed),
    }


def _print_report(res: dict) -> None:
    print("\nskmemory fact extraction (R6)  |  coverage comparison on the same input text\n")
    header = f"{'fixture':<40}{'source':<12}{'regex':>7}{'llm':>7}  llm fact_types"
    print(header)
    print("-" * len(header))
    for row in res["coverage"]:
        source_tag = "live" if row["source"].startswith("live capture") else "synthetic"
        print(
            f"{row['fixture_id']:<40}{source_tag:<12}{row['regex_count']:>7}"
            f"{row['llm_count']:>7}  {row['llm_fact_types']}"
        )
    print(
        "\n(regex = skmemory.extractor.extract_memories; llm = skmemory.fact_extraction "
        "on the recorded response for the same text)\n"
    )

    print("skmemory fact extraction (R6)  |  parse robustness on malformed payloads\n")
    header2 = f"{'case':<40}{'expected':>10}{'actual':>10}{'ok':>6}"
    print(header2)
    print("-" * len(header2))
    for row in res["malformed_payloads"]:
        print(f"{row['label']:<40}{row['expected']:>10}{row['actual']:>10}{str(row['ok']):>6}")
    print(f"\nall rows match expectation: {res['malformed_all_ok']}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="skmemory fact-extraction (R6) bench")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()
    res = run_benchmark()
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=True))
    else:
        _print_report(res)


if __name__ == "__main__":
    main()
