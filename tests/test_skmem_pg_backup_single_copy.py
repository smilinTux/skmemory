"""deploy/ops/skmem-pg-backup.sh in BACKUP_SOP's pg mode: one local copy.

skmem-pg is derived from the flat files, so the standard keeps a single local
dump replaced nightly and nothing off-site. That mode must not promote into
weekly or monthly tiers, must not nag about off-box shipping, and must never
leave zero copies: a failed dump keeps the previous good one.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "ops" / "skmem-pg-backup.sh"


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    # Fake `docker exec ... pg_dump`: DOCKER_FAIL=1 writes a partial and fails.
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'echo "dump-$SKMEM_BACKUP_TS_OVERRIDE"\n'
        'if [ -n "${DOCKER_FAIL:-}" ]; then exit 1; fi\n'
    )
    docker.chmod(0o755)
    dst = tmp_path / "backups"
    e = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "SKMEM_BACKUP_DIR": str(dst),
        "SKMEM_BACKUP_RETAIN": "1",
        "SKMEM_BACKUP_WEEKLY_RETAIN": "0",
        "SKMEM_BACKUP_MONTHLY_RETAIN": "0",
        "SKMEM_BACKUP_OFFBOX": "none",
        # A Sunday that is also the 1st: both promotions would fire.
        "SKMEM_BACKUP_DOW_OVERRIDE": "7",
        "SKMEM_BACKUP_DOM_OVERRIDE": "01",
    }
    return e, dst


def run(env, ts, **over):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={**env, "SKMEM_BACKUP_TS_OVERRIDE": ts, **over},
        capture_output=True,
        text=True,
    )


def test_keeps_exactly_one_copy_and_no_tiers(env):
    e, dst = env
    for ts in ("20261101-000000", "20261102-000000", "20261103-000000"):
        r = run(e, ts)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "WARNING" not in r.stdout + r.stderr
    dumps = sorted(p.name for p in dst.glob("*.dump"))
    assert dumps == ["skmem-pg-skmemory-20261103-000000.dump"]
    assert not (dst / "weekly").exists()
    assert not (dst / "monthly").exists()


def test_failed_dump_keeps_previous_copy(env):
    e, dst = env
    assert run(e, "20261101-000000").returncode == 0
    r = run(e, "20261102-000000", DOCKER_FAIL="1")
    assert r.returncode != 0
    assert sorted(p.name for p in dst.iterdir()) == ["skmem-pg-skmemory-20261101-000000.dump"]
