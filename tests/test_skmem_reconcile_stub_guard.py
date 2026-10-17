"""TDD for scripts/skmem_reconcile.py's stub guard and arg parsing.

Card: skmemory sqlite-only-orphans, 2026-10-16. This script lived
unversioned at ~/skmem-build/skmem_reconcile.py and ran from cron on every
node; a run backfilled 373 rescued SQLite-preview stubs straight into
skmem-pg because nothing checked for the recovery marker before inserting.
It is now under version control here. Importing the module must not execute
the reconcile pipeline (which shells out to `docker exec psql`) -- only
`main()` does that, guarded by `if __name__ == "__main__":`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "skmem_reconcile.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("skmem_reconcile", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["skmem_reconcile"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_import_has_no_side_effects():
    """Importing the module must not shell out to docker/psql -- the whole
    pipeline must live behind `if __name__ == "__main__":`."""
    mod = _load_module()
    assert hasattr(mod, "main")
    assert hasattr(mod, "is_stub_flat")


def test_stub_detected_via_metadata_flag():
    mod = _load_module()
    obj = {"content": "whatever", "metadata": {"recovered_from_sqlite_preview": True}}
    assert mod.is_stub_flat(obj) is True


def test_stub_detected_via_content_marker_alone():
    """Even without the metadata flag, the literal recovery-marker text is
    enough -- defense in depth if metadata is ever stripped en route."""
    mod = _load_module()
    obj = {"content": "short preview text\n\n[recovered from SQLite preview — full content lost]"}
    assert mod.is_stub_flat(obj) is True


def test_real_content_is_not_a_stub():
    mod = _load_module()
    obj = {"content": "a perfectly ordinary real memory", "metadata": {}}
    assert mod.is_stub_flat(obj) is False


def test_non_dict_payload_is_not_a_stub():
    mod = _load_module()
    assert mod.is_stub_flat(None) is False
    assert mod.is_stub_flat("not a dict") is False
    assert mod.is_stub_flat([1, 2, 3]) is False


def test_parse_args_defaults_agent_from_env(monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("SKAGENT", "jarvis")
    agent, force = mod.parse_args([])
    assert agent == "jarvis"
    assert force is False


def test_parse_args_positional_agent_overrides_env(monkeypatch):
    mod = _load_module()
    monkeypatch.setenv("SKAGENT", "jarvis")
    agent, force = mod.parse_args(["lumina"])
    assert agent == "lumina"
    assert force is False


def test_parse_args_force_stubs_flag():
    mod = _load_module()
    agent, force = mod.parse_args(["lumina", "--force-stubs"])
    assert agent == "lumina"
    assert force is True


def test_parse_args_force_stubs_flag_order_independent():
    mod = _load_module()
    agent, force = mod.parse_args(["--force-stubs", "opus"])
    assert agent == "opus"
    assert force is True
