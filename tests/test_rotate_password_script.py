"""Tests for deploy/skmem-pg/rotate-password.sh.

Always run: argument parsing, dry-run (no writes, no mutating docker calls) and the
property that no password or DSN is ever echoed. Run only when the skmem-pg image
exists locally: a full rotation against a throwaway container on a free port, which
never touches the node's real skmem-pg, env file or systemd user manager
(--no-propagate).
"""

import os
import socket
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "skmem-pg" / "rotate-password.sh"
OLD_PW = "oldpw-S3cretValue-0123456789"
IMAGE = "skmem-pg:pg17-bm25-age"


def _run(args, env_extra=None, path_prefix=None):
    env = dict(os.environ)
    if path_prefix:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=300
    )


@pytest.fixture
def stub(tmp_path):
    """A fake docker that logs every call and pretends the container exists."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 0\n')
    docker.chmod(0o755)
    return bindir, log


@pytest.fixture
def envfile(tmp_path):
    p = tmp_path / "pg.env"
    p.write_text(
        "SKMEMORY_VECTOR_BACKEND=pgvector\n"
        f"SKMEMORY_PG_DSN=postgresql://postgres:{OLD_PW}@127.0.0.1:5432/skmemory\n"
    )
    p.chmod(0o600)
    return p


def test_help_exits_zero_and_lists_options():
    r = _run(["--help"])
    assert r.returncode == 0
    for opt in ("--dry-run", "--bind-loopback", "--env-file", "--container", "--restart-units"):
        assert opt in r.stdout


@pytest.mark.parametrize(
    "args,needle",
    [
        (["--bogus"], "unknown argument"),
        (["--port", "abc"], "numeric"),
        (["--env-file"], "needs a value"),
        (["--user", "bad;name"], "must be"),
        (["--remove-old", "skmem-pg"], "does not look like"),
    ],
)
def test_bad_arguments_refused(args, needle, stub):
    r = _run(args, path_prefix=str(stub[0]))
    assert r.returncode != 0
    assert needle in r.stderr


def test_missing_env_file_refused(tmp_path, stub):
    r = _run(["--dry-run", "--env-file", str(tmp_path / "nope.env")], path_prefix=str(stub[0]))
    assert r.returncode != 0
    assert "env file not found" in r.stderr


def test_env_without_password_slot_refused(tmp_path, stub):
    p = tmp_path / "bad.env"
    p.write_text("SKMEMORY_PG_DSN=postgresql://postgres@127.0.0.1:5432/skmemory\n")
    r = _run(["--dry-run", "--env-file", str(p)], path_prefix=str(stub[0]))
    assert r.returncode != 0


def test_dry_run_changes_nothing_and_never_echoes_secret(tmp_path, stub, envfile):
    bindir, log = stub
    backup = tmp_path / "bk"
    before = envfile.read_text()
    r = _run(
        ["--dry-run", "--bind-loopback", "--env-file", str(envfile), "--backup-dir", str(backup)],
        path_prefix=str(bindir),
    )
    assert r.returncode == 0, r.stderr
    assert "dry-run" in r.stdout
    assert envfile.read_text() == before
    assert not backup.exists()
    calls = log.read_text()
    for mutating in ("stop", "rename", "run ", "update", "rm ", "exec", "tag"):
        assert mutating not in calls
    for stream in (r.stdout, r.stderr):
        assert OLD_PW not in stream
        assert "postgresql://" not in stream


def test_remove_old_refuses_running_container(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    d = bindir / "docker"
    d.write_text('#!/usr/bin/env bash\n[ "$1" = inspect ] && echo true\nexit 0\n')
    d.chmod(0o755)
    r = _run(["--remove-old", "skmem-pg-old-1"], path_prefix=str(bindir))
    assert r.returncode != 0
    assert "running" in r.stderr


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _image_present():
    try:
        return (
            subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode
            == 0
        )
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _image_present(), reason=f"{IMAGE} not available locally")
def test_full_rotation_against_throwaway_container(tmp_path):
    name = f"rotate-test-{uuid.uuid4().hex[:8]}"
    vol = f"{name}-vol"
    port = _free_port()
    docker = lambda *a, **k: subprocess.run(["docker", *a], capture_output=True, text=True, **k)  # noqa: E731
    try:
        assert docker("volume", "create", vol).returncode == 0
        r = docker(
            "run", "-d", "--name", name, "--shm-size", "128m",
            "-e", "POSTGRES_DB=skmemory", "-e", f"POSTGRES_PASSWORD={OLD_PW}",
            "-p", f"{port}:5432", "-v", f"{vol}:/var/lib/postgresql/data",
            IMAGE, "postgres", "-c", "shared_preload_libraries=pg_search,age",
        )  # fmt: skip
        assert r.returncode == 0, r.stderr
        import time

        ok = False
        for _ in range(60):
            if docker("exec", name, "pg_isready", "-q", "-U", "postgres").returncode == 0:
                ok = True
                break
            time.sleep(1)
        assert ok
        time.sleep(3)
        sql = (
            "create extension vector; create extension pg_search; create extension age;"
            "create table memories(id int); insert into memories values (1),(2);"
        )
        assert docker("exec", name, "psql", "-U", "postgres", "-d", "skmemory", "-c", sql).returncode == 0
        env = tmp_path / "pg.env"
        env.write_text(f"SKMEMORY_PG_DSN=postgresql://postgres:{OLD_PW}@127.0.0.1:{port}/skmemory\n")
        env.chmod(0o600)
        backup = tmp_path / "bk"
        r = _run([
            "--bind-loopback", "--no-propagate", "--env-file", str(env), "--container", name,
            "--port", str(port), "--backup-dir", str(backup),
        ])  # fmt: skip
        assert r.returncode == 0, r.stdout + r.stderr
        new_pw = (backup / "new-password").read_text().strip()
        assert new_pw and new_pw != OLD_PW
        assert OLD_PW not in env.read_text() and new_pw in env.read_text()
        for stream in (r.stdout, r.stderr):
            assert OLD_PW not in stream and new_pw not in stream
        for f in backup.iterdir():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600
        ports = docker("port", name).stdout
        assert "127.0.0.1" in ports and "0.0.0.0" not in ports
        old_names = docker("ps", "-a", "--filter", f"name={name}-old-", "--format", "{{.Names}}").stdout.split()
        assert len(old_names) == 1
        assert _run(["--remove-old", old_names[0]]).returncode == 0
        assert docker("ps", "-a", "--filter", f"name={name}-old-", "--format", "{{.Names}}").stdout.strip() == ""
    finally:
        names = docker("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}").stdout.split()
        if names:
            docker("rm", "-f", *names)
        docker("volume", "rm", "-f", vol)
