"""scripts/memory-backup.sh: includes tombstones/archive, really encrypts, guards space."""

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "memory-backup.sh"
pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")


@pytest.fixture
def env(tmp_path):
    gh = tmp_path / "gnupg"
    gh.mkdir(mode=0o700)
    e = {**os.environ, "GNUPGHOME": str(gh)}
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-gen-key",
            "t@example.test",
            "default",
            "default",
            "never",
        ],
        env=e,
        check=True,
        capture_output=True,
    )
    agent = tmp_path / "agent"
    for rel in ("short-term", "tombstones", "archive"):
        d = agent / "memory" / rel
        d.mkdir(parents=True)
        (d / f"{rel}-1.json").write_text(f'{{"id": "{rel}-1"}}')
    (agent / "seeds").mkdir()
    (agent / "seeds" / "s.json").write_text("{}")
    e.update(
        AGENT_DIR=str(agent),
        BACKUP_BASE=str(tmp_path / "bk"),
        CLAWD_DIR=str(tmp_path / "nowhere"),
        BACKUP_RECIPIENT="t@example.test",
        MIN_FREE_MB="1",
    )
    return e


def run(env, **over):
    return subprocess.run(
        ["bash", str(SCRIPT)], env={**env, **over}, capture_output=True, text=True
    )


def test_encrypted_and_contains_tombstones_and_archive(env):
    r = run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    files = list(Path(env["BACKUP_BASE"], "daily").iterdir())
    assert [f.name.endswith(".tar.gz.gpg") for f in files] == [True]
    raw = files[0].read_bytes()
    assert not raw.startswith(b"\x1f\x8b"), "archive is plain gzip"
    plain = subprocess.run(
        ["gpg", "--batch", "-d", str(files[0])], env=env, capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tf:
        names = tf.getnames()
    assert "memory/tombstones/tombstones-1.json" in names
    assert "memory/archive/archive-1.json" in names
    assert "memory/short-term/short-term-1.json" in names


def test_missing_key_refuses_plaintext(env):
    r = run(env, BACKUP_RECIPIENT="nobody@example.test")
    assert r.returncode != 0
    assert not list(Path(env["BACKUP_BASE"], "daily").glob("*"))


def test_free_space_guard_fails_without_writing(env):
    r = run(env, MIN_FREE_MB="999999999")
    assert r.returncode != 0 and "not enough free space" in r.stdout + r.stderr
    assert not list(Path(env["BACKUP_BASE"], "daily").glob("*"))


def _names(env):
    files = list(Path(env["BACKUP_BASE"], "daily").iterdir())
    plain = subprocess.run(
        ["gpg", "--batch", "-d", str(files[0])], env=env, capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tf:
        return tf.getnames()


def test_source_state_included_and_sqlite_index_never(env):
    mem = Path(env["AGENT_DIR"], "memory")
    (mem / "index.db").write_text("derived")
    (mem / "intentions").mkdir()
    (mem / "intentions" / "i.json").write_text("{}")
    (mem / "promotion-log.json").write_text("{}")
    r = run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    names = _names(env)
    assert "memory/intentions/i.json" in names
    assert "memory/promotion-log.json" in names
    assert not [n for n in names if n.startswith("memory/index.db")]


@pytest.fixture
def restic_env(env, tmp_path):
    """A fake restic on PATH that logs each call; RESTIC_FAIL=1 fails backup."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "restic.log"
    fake = bindir / "restic"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        'if [ "$1" = backup ] && [ -n "${RESTIC_FAIL:-}" ]; then exit 1; fi\n'
        "exit 0\n"
    )
    fake.chmod(0o755)
    creds = tmp_path / "restic.env"
    creds.write_text("RESTIC_REPOSITORY=/nowhere\nRESTIC_PASSWORD=x\n")
    ws = Path(env["AGENT_DIR"], "memory", "worship-sessions")
    ws.mkdir()
    (ws / "a.wav").write_text("media")
    return {
        **env,
        "PATH": f"{bindir}:{env['PATH']}",
        "MEMORY_BACKUP_RESTIC_ENV": str(creds),
    }, log


def test_offsite_snapshot_carries_source_and_gfs_retention(restic_env):
    e, log = restic_env
    r = run(e)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    backup = next(c for c in calls if c.startswith("backup"))
    forget = next(c for c in calls if c.startswith("forget"))
    assert "--tag skmem-flat-agent" in backup
    assert "/memory/tombstones" in backup and "/memory/archive" in backup
    assert "/memory/worship-sessions" in backup
    assert "index.db" not in backup
    assert "--keep-daily 7 --keep-weekly 4 --keep-monthly 12" in forget
    # worship-sessions is off-site only, never in the nightly tarball.
    assert not [n for n in _names(e) if "worship-sessions" in n]


def test_offsite_failure_fails_the_run_but_keeps_the_local_tarball(restic_env):
    e, _ = restic_env
    r = run(e, RESTIC_FAIL="1")
    assert r.returncode != 0
    assert "off-site restic snapshot failed" in r.stdout + r.stderr
    assert len(list(Path(e["BACKUP_BASE"], "daily").glob("*.tar.gz.gpg"))) == 1


def test_unreadable_restic_env_fails(env):
    r = run(env, MEMORY_BACKUP_RESTIC_ENV="/nonexistent/restic.env")
    assert r.returncode != 0 and "restic env not readable" in r.stdout + r.stderr
