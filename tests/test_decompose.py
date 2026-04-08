"""Tests for SKMemory document decomposition."""

from skmemory.decompose import decompose_content


def test_decompose_extracts_chunks_and_signals() -> None:
    content = """
# Contract Notice

Section 1. The Internal Revenue Service shall respond within 10 days.
Under 26 U.S.C. § 6903, the fiduciary relationship must be recognized.

## Demand

Chef Casey and King Aster therefore claim the account is settled.
UCC § 3-301 establishes holder rights in the instrument.
""".strip()

    result = decompose_content(content, chunk_target=120, chunk_overlap=20)

    assert len(result.chunks) >= 2
    assert "Contract Notice" in result.section_titles
    assert any("26 U.S.C. § 6903" in citation for citation in result.citations)
    assert any("UCC § 3-301" in citation for citation in result.citations)
    assert any("Internal Revenue Service" in entity for entity in result.entities)
    assert all("\n" not in entity for entity in result.entities)
    assert any("shall respond" in claim.lower() for claim in result.claims)
