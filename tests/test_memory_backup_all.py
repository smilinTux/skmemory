"""scripts/memory-backup-all.sh: one job backs up every agent on the node."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "memory-backup-all.sh"
pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg not installed")


def _agent(root, name, tier="short-term"):
    d = root / name / "memory" / tier
    d.mkdir(parents=True)
    (d / f"{name}-1.json").write_text(f'{{"id": "{name}-1"}}')


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
    root = tmp_path / "agents"
    _agent(root, "lumina")
    _agent(root, "opus", "tombstones")
    (root / "empty" / "memory" / "short-term").mkdir(parents=True)
    (root / "alias").symlink_to(root / "lumina")
    ws = tmp_path / "clawd"
    ws.mkdir()
    (ws / "SOUL.md").write_text("soul")
    e.pop("MEMORY_BACKUP_RESTIC_ENV", None)
    e.update(
        AGENTS_ROOT=str(root),
        BACKUP_BASE_ROOT=str(tmp_path / "bk"),
        CLAWD_DIR=str(ws),
        BACKUP_RECIPIENT="t@example.test",
        MIN_FREE_MB="1",
    )
    return e


def run(env, **over):
    return subprocess.run(
        ["bash", str(SCRIPT)], env={**env, **over}, capture_output=True, text=True
    )


def _tar_names(env, f):
    plain = subprocess.run(
        ["gpg", "--batch", "-d", str(f)], env=env, capture_output=True, check=True
    ).stdout
    return (
        subprocess.run(["tar", "-tz"], input=plain, capture_output=True, check=True)
        .stdout.decode()
        .split()
    )


def test_every_agent_with_memory_gets_its_own_named_backup(env):
    r = run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    bk = Path(env["BACKUP_BASE_ROOT"])
    assert sorted(p.name for p in bk.iterdir()) == ["lumina-memory", "opus-memory"]
    (lum,) = (bk / "lumina-memory" / "daily").iterdir()
    (op,) = (bk / "opus-memory" / "daily").iterdir()
    assert lum.name.startswith("lumina-memory-") and op.name.startswith("opus-memory-")
    # The workspace md files belong to one agent only.
    assert "SOUL.md" in _tar_names(env, lum)
    assert "SOUL.md" not in _tar_names(env, op)
    assert "memory/tombstones/opus-1.json" in _tar_names(env, op)
    assert "empty" in r.stdout and "alias" in r.stdout


def test_skip_list(env):
    r = run(env, MEMORY_BACKUP_SKIP="opus")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not Path(env["BACKUP_BASE_ROOT"], "opus-memory").exists()


def test_one_failure_fails_the_run_but_the_others_still_back_up(env):
    bk = Path(env["BACKUP_BASE_ROOT"])
    bk.mkdir()
    (bk / "lumina-memory").write_text("not a dir")  # mkdir inside fails
    r = run(env)
    assert r.returncode != 0 and "FAILED 1: lumina" in r.stdout + r.stderr
    assert len(list((bk / "opus-memory" / "daily").glob("*.tar.gz.gpg"))) == 1


def test_nothing_to_back_up_fails(env, tmp_path):
    bare = tmp_path / "bare"
    (bare / "x" / "memory").mkdir(parents=True)
    r = run(env, AGENTS_ROOT=str(bare))
    assert r.returncode != 0 and "nothing was backed up" in r.stdout + r.stderr


def test_restic_tags_per_agent_and_one_prune(env, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "restic.log"
    fake = bindir / "restic"
    fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 0\n')
    fake.chmod(0o755)
    creds = tmp_path / "restic.env"
    creds.write_text("RESTIC_REPOSITORY=/nowhere\nRESTIC_PASSWORD=x\n")
    r = run(env, PATH=f"{bindir}:{env['PATH']}", MEMORY_BACKUP_RESTIC_ENV=str(creds))
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    backups = [c for c in calls if c.startswith("backup")]
    forgets = [c for c in calls if c.startswith("forget")]
    assert len(backups) == 2
    assert any("--tag skmem-flat-lumina" in c for c in backups)
    assert any("--tag skmem-flat-opus" in c for c in backups)
    assert forgets and not [c for c in forgets if "--prune" in c]
    assert [c for c in calls if c.startswith("prune")] == ["prune --quiet"]
