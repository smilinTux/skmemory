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
        (d / f"{rel}-1.json").write_text('{"id": "%s-1"}' % rel)
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
    names = tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz").getnames()
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
