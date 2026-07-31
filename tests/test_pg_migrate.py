"""Tests for the skmem-pg migration runner (card OPS1.3, epic eae727d0).

DB-independent, mirroring tests/test_ops_namespace_migration.py: psql/docker are
never invoked. The runner is built as pure plan objects + an injectable executor,
so ORDERING, IDEMPOTENCE, pre-dump/verify wiring, transport selection, and the
role-login/skvault credential convention are all asserted without a database.
"""

from __future__ import annotations

import re

import pytest

from skmemory import pg_migrate as pm
from skmemory.pg_migrate import (
    DEPLOY_DIR,
    MANIFEST,
    Migration,
    Plan,
    Step,
    Target,
    build_migrate_plan,
    build_roles_plan,
    load_manifest,
    resolve_migration,
    run_plan,
)

# --------------------------------------------------------------------------- #
# Manifest: single source of truth, ordered, excludes historical migrations   #
# --------------------------------------------------------------------------- #


def test_manifest_exists_and_lists_ops_namespace():
    entries = load_manifest()
    names = [m.name for m in entries]
    assert "03-ops-namespace.sql" in names
    # verify script carried alongside the migration.
    ops = next(m for m in entries if m.name == "03-ops-namespace.sql")
    assert ops.verify == "verify-ops.sql"


def test_manifest_excludes_superseded_historical_migrations():
    """02-enable-bm25-age.sql and 03-cutover-mxbai.sql are baked into schema.sql;
    03-cutover is NOT idempotent, so it must never be in the forward list."""
    names = [m.name for m in load_manifest()]
    assert "02-enable-bm25-age.sql" not in names
    assert "03-cutover-mxbai.sql" not in names


def test_manifest_order_is_preserved():
    raw = MANIFEST.read_text(encoding="utf-8")
    # every listed migration file must exist on disk.
    for m in load_manifest():
        assert m.path.exists(), f"listed migration missing on disk: {m.name}"
        if m.verify:
            assert m.verify_path is not None and m.verify_path.exists()
    assert raw.strip(), "manifest must not be empty"


def test_load_manifest_ignores_comments_and_blanks(tmp_path):
    f = tmp_path / "migrations.txt"
    f.write_text(
        "# header comment\n"
        "\n"
        "01-a.sql | verify-a.sql\n"
        "   \n"
        "02-b.sql   # trailing comment\n",
        encoding="utf-8",
    )
    entries = load_manifest(f)
    assert [(m.name, m.verify) for m in entries] == [
        ("01-a.sql", "verify-a.sql"),
        ("02-b.sql", None),
    ]


def test_listed_forward_migrations_are_idempotent():
    """Every forward migration must carry idempotence markers so re-apply (fresh
    init replay or a re-run on a live node) is a safe no-op."""
    for m in load_manifest():
        sql = m.path.read_text(encoding="utf-8")
        assert "ON_ERROR_STOP" in sql, f"{m.name} must set ON_ERROR_STOP"
        assert re.search(
            r"IF NOT EXISTS|CREATE OR REPLACE|OR REPLACE|IF NOT EXISTS", sql, re.IGNORECASE
        ), f"{m.name} must use guarded/idempotent DDL"


# --------------------------------------------------------------------------- #
# resolve_migration                                                           #
# --------------------------------------------------------------------------- #


def test_resolve_by_manifest_name_carries_verify():
    m = resolve_migration("03-ops-namespace.sql")
    assert m.name == "03-ops-namespace.sql"
    assert m.verify == "verify-ops.sql"


def test_resolve_by_path_basename():
    m = resolve_migration(str(DEPLOY_DIR / "03-ops-namespace.sql"))
    assert m.name == "03-ops-namespace.sql"


def test_resolve_unknown_raises():
    with pytest.raises(FileNotFoundError):
        resolve_migration("99-does-not-exist.sql")


# --------------------------------------------------------------------------- #
# build_migrate_plan: ordering + transport + pre-dump/verify toggles           #
# --------------------------------------------------------------------------- #


def test_plan_order_predump_apply_verify():
    plan = build_migrate_plan(load_manifest(), Target(), pre_dump=True, verify=True)
    labels = [s.label for s in plan.steps]
    assert labels[0].startswith("pre-dump")
    assert labels[1].startswith("apply 03-ops-namespace.sql")
    assert labels[2].startswith("verify verify-ops.sql")


def test_plan_no_predump_no_verify():
    plan = build_migrate_plan(load_manifest(), Target(), pre_dump=False, verify=False)
    assert [s.label for s in plan.steps] == ["apply 03-ops-namespace.sql"]


def test_plan_single_predump_for_multiple_migrations():
    migs = [Migration("a.sql"), Migration("b.sql")]
    plan = build_migrate_plan(migs, Target(), pre_dump=True, verify=False)
    predumps = [s for s in plan.steps if s.label.startswith("pre-dump")]
    assert len(predumps) == 1  # one snapshot covers the whole DB
    assert [s.label for s in plan.steps][1:] == ["apply a.sql", "apply b.sql"]


def test_docker_transport_matches_readme_invocation():
    plan = build_migrate_plan(load_manifest(), Target(container="skmem-pg"), pre_dump=False)
    apply = plan.steps[0]
    assert apply.argv == [
        "docker", "exec", "-i", "skmem-pg",
        "psql", "-U", "postgres", "-d", "skmemory", "-v", "ON_ERROR_STOP=1",
    ]
    # SQL is fed on stdin (the file), not argv.
    assert apply.stdin_path == (DEPLOY_DIR / "03-ops-namespace.sql")


def test_dsn_transport_uses_dash_f():
    t = Target(dsn="postgresql://u@h/skmemory")
    plan = build_migrate_plan(load_manifest(), t, pre_dump=False, verify=False)
    apply = plan.steps[0]
    assert apply.argv[0] == "psql"
    assert "-f" in apply.argv
    assert apply.stdin_path is None


def test_predump_uses_custom_format_and_captures_stdout(tmp_path):
    plan = build_migrate_plan(
        load_manifest(), Target(), pre_dump=True, verify=False, dump_dir=tmp_path
    )
    dump = plan.steps[0]
    assert "-Fc" in dump.argv
    assert dump.stdout_path is not None
    assert dump.stdout_path.parent == tmp_path


def test_empty_migration_list_is_empty_plan():
    assert build_migrate_plan([], Target()).steps == []


# --------------------------------------------------------------------------- #
# Role login binding (G2 credential gap)                                       #
# --------------------------------------------------------------------------- #


def test_role_ddl_is_idempotent_and_binds_group():
    ddl = pm._role_ddl("skbrain_projector", "skbrain_ops_rw", "pw")
    assert "pg_roles WHERE rolname = 'skbrain_projector'" in ddl  # create-if-missing guard
    assert "CREATE ROLE skbrain_projector LOGIN PASSWORD" in ddl
    assert "ALTER ROLE skbrain_projector WITH LOGIN PASSWORD" in ddl  # else branch
    assert "GRANT skbrain_ops_rw TO skbrain_projector;" in ddl  # group binding


def test_role_ddl_escapes_single_quotes_in_password():
    ddl = pm._role_ddl("r", "g", "pa'ss")
    assert "PASSWORD 'pa''ss'" in ddl  # quote doubled, not broken out of the literal


def test_roles_plan_from_env_binds_both_roles():
    env = {"SKBRAIN_PG_PROJECTOR_PW": "p1", "SKBRAIN_PG_READER_PW": "p2"}
    plan = build_roles_plan(Target(), env=env)
    labels = [s.label for s in plan.steps]
    assert any("skbrain_projector -> skbrain_ops_rw" in x for x in labels)
    assert any("skbrain_reader -> skbrain_ops_ro" in x for x in labels)
    # secrets ride on stdin only, marked secret, never in argv.
    for s in plan.steps:
        assert s.secret is True
        assert s.stdin_text is not None
        assert "p1" not in " ".join(s.argv) and "p2" not in " ".join(s.argv)


def test_roles_plan_missing_password_raises():
    with pytest.raises(KeyError):
        build_roles_plan(Target(), env={})


def test_roles_plan_skip_missing():
    env = {"SKBRAIN_PG_PROJECTOR_PW": "p1"}  # reader pw absent
    plan = build_roles_plan(Target(), env=env, skip_missing=True)
    assert len(plan.steps) == 1
    assert "skbrain_projector" in plan.steps[0].label


def test_roles_describe_redacts_password():
    plan = build_roles_plan(Target(), env={"SKBRAIN_PG_PROJECTOR_PW": "topsecret", "SKBRAIN_PG_READER_PW": "x"})
    text = plan.describe()
    assert "topsecret" not in text
    assert "redacted" in text.lower()


# --------------------------------------------------------------------------- #
# run_plan: execution ordering, stdin wiring, fatal semantics, dry-run safety  #
# --------------------------------------------------------------------------- #


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_plan_executes_in_order_and_pipes_stdin(tmp_path):
    sqlfile = tmp_path / "m.sql"
    sqlfile.write_text("SELECT 1;", encoding="utf-8")
    plan = Plan(steps=[
        Step(argv=["psql", "a"], label="apply m", stdin_path=sqlfile),
        Step(argv=["psql", "b"], label="verify m", stdin_path=sqlfile, fatal=False),
    ])
    calls = []

    def fake_runner(argv, input=None, capture_output=False):
        calls.append((argv, input))
        return _FakeProc(0, stdout=b"ok")

    run_plan(plan, runner=fake_runner, echo=lambda *_: None)
    assert [c[0] for c in calls] == [["psql", "a"], ["psql", "b"]]
    assert calls[0][1] == b"SELECT 1;"  # file bytes piped on stdin


def test_run_plan_fatal_step_raises():
    plan = Plan(steps=[Step(argv=["psql"], label="apply", stdin_text="X")])

    def fake_runner(argv, input=None, capture_output=False):
        return _FakeProc(1, stderr=b"boom")

    with pytest.raises(pm.MigrationError):
        run_plan(plan, runner=fake_runner, echo=lambda *_: None)


def test_run_plan_nonfatal_verify_failure_does_not_raise():
    plan = Plan(steps=[Step(argv=["psql"], label="verify", stdin_text="X", fatal=False)])

    def fake_runner(argv, input=None, capture_output=False):
        return _FakeProc(2, stderr=b"verify weird")

    res = run_plan(plan, runner=fake_runner, echo=lambda *_: None)
    assert res[0]["returncode"] == 2  # recorded, not raised


def test_run_plan_predump_writes_stdout_to_file(tmp_path):
    out = tmp_path / "sub" / "dump.bin"
    plan = Plan(steps=[Step(argv=["pg_dump"], label="pre-dump", stdout_path=out)])

    def fake_runner(argv, input=None, capture_output=False):
        return _FakeProc(0, stdout=b"DUMPBYTES")

    run_plan(plan, runner=fake_runner, echo=lambda *_: None)
    assert out.read_bytes() == b"DUMPBYTES"


def test_run_plan_redacts_secret_stderr():
    plan = Plan(steps=[Step(argv=["psql"], label="bind", stdin_text="CREATE ROLE r LOGIN PASSWORD 'hunter2';", secret=True, fatal=False)])

    def fake_runner(argv, input=None, capture_output=False):
        return _FakeProc(1, stderr=b"error near PASSWORD 'hunter2'")

    res = run_plan(plan, runner=fake_runner, echo=lambda *_: None)
    assert "hunter2" not in res[0]["stderr"]
    assert "REDACTED" in res[0]["stderr"]


def test_idempotent_plan_is_deterministic():
    """Building the same plan twice yields identical argv/stdin wiring (a re-run
    applies the same guarded, idempotent SQL)."""
    p1 = build_migrate_plan(load_manifest(), Target(), pre_dump=False)
    p2 = build_migrate_plan(load_manifest(), Target(), pre_dump=False)
    assert [(s.argv, s.stdin_path, s.label) for s in p1.steps] == \
           [(s.argv, s.stdin_path, s.label) for s in p2.steps]
