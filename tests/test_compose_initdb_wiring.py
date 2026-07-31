"""Structural tests for the compose first-boot migration wiring (card OPS1.3).

DB/docker-independent: validates that a fresh `docker compose up` applies the
base schema AND every forward migration in order, closing G2's "fresh compose
omits the DDL" gap. Parses docker-compose.yml, the init wrapper, and the
migration manifest; runs no container.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"
WRAPPER = REPO / "deploy" / "skmem-pg" / "initdb" / "00-run-init.sh"
MANIFEST = REPO / "deploy" / "skmem-pg" / "migrations.txt"


def _service() -> dict:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data["services"]["skmem-pg"]


# --------------------------------------------------------------------------- #
# Compose mounts                                                              #
# --------------------------------------------------------------------------- #


def test_init_wrapper_mounted_into_initdb_dir():
    vols = _service()["volumes"]
    assert any(
        v.endswith("/docker-entrypoint-initdb.d/00-run-init.sh:ro")
        and "00-run-init.sh:" in v
        for v in vols
    ), "the init wrapper must be mounted into /docker-entrypoint-initdb.d"


def test_source_dir_mounted_readonly_for_wrapper():
    vols = _service()["volumes"]
    assert any(
        v.endswith(":/skmem-initdb-src:ro") for v in vols
    ), "deploy/skmem-pg must be mounted read-only as the wrapper's source"


def test_schema_not_double_mounted_as_raw_init_file():
    """schema.sql must be applied BY the wrapper, not also mounted directly as a
    numbered init file (which would double-apply or run out of order)."""
    vols = _service()["volumes"]
    assert not any(
        "schema.sql:/docker-entrypoint-initdb.d/" in v for v in vols
    ), "schema.sql must not be mounted directly into initdb.d anymore"


# --------------------------------------------------------------------------- #
# Init wrapper                                                                #
# --------------------------------------------------------------------------- #


def test_wrapper_exists_and_is_failsafe():
    txt = WRAPPER.read_text(encoding="utf-8")
    assert "set -euo pipefail" in txt, "wrapper must be fail-safe"
    assert "ON_ERROR_STOP=1" in txt, "every psql apply must abort on first error"


def test_wrapper_applies_schema_before_migrations():
    txt = WRAPPER.read_text(encoding="utf-8")
    schema_at = txt.index("schema.sql")
    manifest_at = txt.index("migrations.txt")
    assert schema_at < manifest_at, "schema.sql must be applied before the migration loop"


def test_wrapper_reads_the_shared_manifest():
    txt = WRAPPER.read_text(encoding="utf-8")
    assert "migrations.txt" in txt, "wrapper must drive off migrations.txt (single source of truth)"


# --------------------------------------------------------------------------- #
# Manifest excludes the historical/non-idempotent migrations                  #
# --------------------------------------------------------------------------- #


def test_manifest_lists_ops_and_excludes_historical():
    txt = MANIFEST.read_text(encoding="utf-8")
    # active line (uncommented) for the ops migration.
    active = [
        ln.split("#", 1)[0].strip()
        for ln in txt.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    joined = "\n".join(active)
    assert "03-ops-namespace.sql" in joined
    assert "03-cutover-mxbai.sql" not in joined, "non-idempotent cutover must not auto-run"
    assert "02-enable-bm25-age.sql" not in joined, "superseded historical migration must not auto-run"
