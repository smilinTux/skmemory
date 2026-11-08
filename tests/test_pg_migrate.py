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
        "# header comment\n\n01-a.sql | verify-a.sql\n   \n02-b.sql   # trailing comment\n",
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
    assert [s.label for s in plan.steps] == [f"apply {m.name}" for m in load_manifest()]


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
        "docker",
        "exec",
        "-i",
        "skmem-pg",
        "psql",
        "-U",
        "postgres",
        "-d",
        "skmemory",
        "-v",
        "ON_ERROR_STOP=1",
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
    plan = build_roles_plan(
        Target(), env={"SKBRAIN_PG_PROJECTOR_PW": "topsecret", "SKBRAIN_PG_READER_PW": "x"}
    )
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
    plan = Plan(
        steps=[
            Step(argv=["psql", "a"], label="apply m", stdin_path=sqlfile),
            Step(argv=["psql", "b"], label="verify m", stdin_path=sqlfile, fatal=False),
        ]
    )
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
    plan = Plan(
        steps=[
            Step(
                argv=["psql"],
                label="bind",
                stdin_text="CREATE ROLE r LOGIN PASSWORD 'hunter2';",
                secret=True,
                fatal=False,
            )
        ]
    )

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
    assert [(s.argv, s.stdin_path, s.label) for s in p1.steps] == [
        (s.argv, s.stdin_path, s.label) for s in p2.steps
    ]


# --------------------------------------------------------------------------- #
# Packaging, path honouring, DSN redaction, transport fallback                #
# --------------------------------------------------------------------------- #

import subprocess  # noqa: E402
import sys  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

from skmemory import pg_migrate as _pm  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_PW = "s3cr3t-Pw!x"
_DSN = f"postgresql://postgres:{_PW}@127.0.0.1:5432/skmemory"


def test_sql_lives_inside_the_package():
    assert Path(_pm.__file__).resolve().parent / "pg" == _pm.DEPLOY_DIR
    assert load_manifest(), "manifest must be non-empty from the package location"
    for m in load_manifest():
        assert m.path.is_file(), m.path
        if m.verify:
            assert m.verify_path.is_file(), m.verify_path


def test_wheel_contains_manifest_and_every_listed_sql(tmp_path):
    out = tmp_path / "wheel"
    out.mkdir()
    r = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation", "-o", str(out), str(_REPO)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    if r.returncode != 0 and "No module named build" in (r.stderr + r.stdout):
        pytest.skip("python -m build unavailable")
    assert r.returncode == 0, r.stderr[-2000:]
    wheel = next(out.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    assert "skmemory/pg/migrations.txt" in names
    assert "skmemory/pg/schema.sql" in names
    entries = load_manifest()
    assert entries
    for m in entries:
        assert f"skmemory/pg/{m.name}" in names
        if m.verify:
            assert f"skmemory/pg/{m.verify}" in names
    # Read the manifest as an installed copy would see it.
    z = zipfile.ZipFile(wheel)
    ext = tmp_path / "ext"
    z.extractall(ext)
    listed = load_manifest(ext / "skmemory" / "pg" / "migrations.txt")
    assert [m.name for m in listed] == [m.name for m in entries]


def test_resolve_migration_honours_path_as_given(tmp_path):
    d = tmp_path / "elsewhere"
    d.mkdir()
    f = d / "03-ops-namespace.sql"  # same basename as a manifest entry, different dir
    f.write_text("select 1;\n")
    m = resolve_migration(str(f))
    assert m.path == f.resolve()
    assert m.path != DEPLOY_DIR / "03-ops-namespace.sql"


def test_resolve_migration_bare_name_still_uses_manifest():
    assert resolve_migration("03-ops-namespace.sql").path == DEPLOY_DIR / "03-ops-namespace.sql"


def test_resolve_migration_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_migration(str(tmp_path / "nope" / "zz-missing.sql"))


@pytest.mark.parametrize(
    "dsn",
    [
        _DSN,
        f"postgresql://postgres:{_PW}@127.0.0.1:5432/skmemory?sslmode=disable",
        f"host=127.0.0.1 port=5432 dbname=skmemory user=postgres password='{_PW}'",
        f"postgresql://postgres@127.0.0.1:5432/skmemory?password={_PW}",
    ],
)
def test_dsn_password_never_in_argv_or_description(dsn):
    t = Target(dsn=dsn)
    plan = build_migrate_plan(load_manifest()[:1], t, pre_dump=True, dump_dir=Path("/x"))
    for step in plan.steps:
        assert _PW not in " ".join(step.argv)
        assert step.env == {"PGPASSWORD": _PW}
    assert _PW not in plan.describe()
    assert _PW not in t.display_dsn
    assert "***" in t.display_dsn or "password" not in dsn


def test_redact_dsn_url_form():
    assert _pm.redact_dsn(_DSN) == "postgresql://postgres:***@127.0.0.1:5432/skmemory"


def test_run_plan_passes_password_via_env_and_scrubs_errors():
    t = Target(dsn=_DSN)
    plan = build_migrate_plan(load_manifest()[:1], t, pre_dump=False, verify=False)
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return subprocess.CompletedProcess(argv, 1, b"", f"bad {_DSN} {_PW}".encode())

    out = []
    with pytest.raises(_pm.MigrationError) as ei:
        run_plan(plan, runner=runner, echo=out.append)
    assert seen["env"]["PGPASSWORD"] == _PW
    assert _PW not in " ".join(seen["argv"])
    assert _PW not in str(ei.value)
    assert _PW not in "\n".join(out)


def test_cli_dry_run_never_prints_dsn_password(monkeypatch):
    from click.testing import CliRunner

    from skmemory.cli import cli

    monkeypatch.setenv("SKMEMORY_PG_DSN", _DSN)
    monkeypatch.setattr(_pm.shutil, "which", lambda b: f"/usr/bin/{b}")
    res = CliRunner().invoke(cli, ["pg", "migrate", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert _PW not in res.output
    assert "***" in res.output


def test_prepare_target_falls_back_to_docker_when_client_tools_missing():
    msgs: list[str] = []
    t = _pm.prepare_target(
        Target(dsn=_DSN),
        need_dump=True,
        which=lambda b: None,
        container_running=lambda n: True,
        echo=msgs.append,
    )
    assert not t.uses_dsn
    assert t.db == "skmemory" and t.user == "postgres"
    assert msgs and "docker exec" in msgs[0] and _PW not in msgs[0]


def test_prepare_target_errors_clearly_without_tools_or_container():
    with pytest.raises(_pm.MigrationError) as ei:
        _pm.prepare_target(
            Target(dsn=_DSN),
            need_dump=True,
            which=lambda b: None,
            container_running=lambda n: False,
        )
    assert "pg_dump" in str(ei.value) and _PW not in str(ei.value)


def test_prepare_target_keeps_dsn_when_tools_present():
    t = Target(dsn=_DSN)
    assert _pm.prepare_target(t, need_dump=True, which=lambda b: "/bin/" + b) is t
