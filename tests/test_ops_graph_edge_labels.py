"""07-ops-graph-edge-labels.sql: every skbrain page edge type has an ops_brain label."""

from __future__ import annotations

import re
from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[1] / "skmemory" / "pg"

# skbrain page-frontmatter-v1 edge types (schemas/page-frontmatter-v1.schema.json).
PAGE_EDGE_TYPES = {
    "remediates",
    "touches",
    "depends_on",
    "runs_on",
    "hosts",
    "connects_to",
    "documents",
    "about",
    "promoted",
}


def _labels(name: str) -> set[str]:
    text = (MIGRATION / name).read_text()
    arrays = re.findall(r"FOREACH edge IN ARRAY ARRAY\[([^\]]*)\]", text)
    return {x.strip().strip("'") for a in arrays for x in a.split(",")}


def test_manifest_lists_07_after_05():
    lines = [
        ln
        for ln in (MIGRATION / "migrations.txt").read_text().splitlines()
        if ln and not ln.startswith("#")
    ]
    assert "07-ops-graph-edge-labels.sql | verify-ops-graph-edge-labels.sql" in lines
    names = [ln.split("|")[0].strip() for ln in lines]
    assert names.index("07-ops-graph-edge-labels.sql") > names.index("05-ops-graph-grants.sql")


def test_every_page_edge_type_has_a_label():
    labels = _labels("05-ops-graph-grants.sql") | _labels("07-ops-graph-edge-labels.sql")
    assert {t.upper() for t in PAGE_EDGE_TYPES} <= labels
    assert "LINKS_TO" in labels  # the projector's fallback


def test_07_is_idempotent_and_regrants():
    text = (MIGRATION / "07-ops-graph-edge-labels.sql").read_text()
    assert "IF NOT EXISTS" in text and "create_elabel" in text
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops_brain TO skbrain_ops_rw"
        in text
    )
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA ops_brain FROM PUBLIC" in text
    assert not _labels("07-ops-graph-edge-labels.sql") & _labels("05-ops-graph-grants.sql")
