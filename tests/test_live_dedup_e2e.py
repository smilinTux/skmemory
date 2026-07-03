"""Live end-to-end test for advisory dedup on the LIVE pgvector backend.

Unlike ``test_chroma_dedup.py`` (mocked collection, no real embeddings),
this test exercises the REAL path: real Postgres/pgvector (skmem-pg) +
real mxbai-embed-large embeddings over the network. It proves
``PGVectorBackend.find_similar()`` actually works against the backend that
is live in production (``SKMEMORY_VECTOR_BACKEND=pgvector``), closing the
gap where ``find_similar`` previously only existed on the Chroma backend
and dedup was silently a no-op live.

Safety: writes ONLY under a throwaway ``agent`` value scoped to this
process's PID, and deletes every row for that agent in teardown. Every
query against the `memories` table is filtered by `agent=%s` at the SQL
level inside PGVectorBackend itself, so this test structurally cannot
touch real agents' rows (lumina, opus, etc.) even on failure paths.

Skipped automatically (module-level) when either skmem-pg or the mxbai
embed endpoint is unreachable, so this stays green in CI/offline runs.
"""

from __future__ import annotations

import os

import pytest

PG_DSN = os.environ.get(
    "SKMEMORY_PG_DSN", "postgresql://postgres:skmemory@localhost:5432/skmemory"
)
EMBED_URL = os.environ.get("SKMEMORY_EMBED_URL", "http://192.168.0.100:11434/api/embed")
EMBED_MODEL = os.environ.get("SKMEMORY_EMBED_MODEL", "mxbai-embed-large")

DEFAULT_THRESHOLD = 0.73


def _pg_reachable() -> bool:
    try:
        import psycopg

        conn = psycopg.connect(PG_DSN, autocommit=True, connect_timeout=5)
        conn.close()
        return True
    except Exception:
        return False


def _embed_reachable() -> bool:
    try:
        import httpx

        r = httpx.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "input": "reachability probe", "truncate": True},
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
        return bool(data.get("embeddings") or data.get("data") or data.get("embedding"))
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not (_pg_reachable() and _embed_reachable()),
    reason="skmem-pg (SKMEMORY_PG_DSN) or the mxbai embed endpoint (SKMEMORY_EMBED_URL) "
    "is unreachable -- skipping live e2e dedup test.",
)


@pytest.fixture
def throwaway_agent() -> str:
    return f"__dedup_test_{os.getpid()}__"


@pytest.fixture
def pg_backend(throwaway_agent: str):
    """A REAL PGVectorBackend against live skmem-pg, scoped to a throwaway agent.

    Uses the backend's own real embed_fn (real HTTP call to mxbai) -- no
    mocking. Teardown deletes every row for the throwaway agent, and only
    that agent, from the live `memories` table.
    """
    from skmemory.backends.pgvector_backend import PGVectorBackend

    backend = PGVectorBackend(
        dsn=PG_DSN,
        embed_url=EMBED_URL,
        embed_model=EMBED_MODEL,
        agent=throwaway_agent,
    )

    yield backend

    # Teardown: delete ONLY this throwaway agent's rows. Never touches any
    # other agent value.
    conn = backend._connection()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories WHERE agent = %s", (throwaway_agent,))
    conn.close()


@pytest.fixture
def store(pg_backend, tmp_path):
    from skmemory.backends.file_backend import FileBackend
    from skmemory.store import MemoryStore

    return MemoryStore(primary=FileBackend(base_path=str(tmp_path / "memories")), vector=pg_backend)


def _live_memories_count() -> int:
    import psycopg

    conn = psycopg.connect(PG_DSN, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM memories")
            return cur.fetchone()[0]
    finally:
        conn.close()


class TestLiveDedupEndToEnd:
    def test_paraphrase_is_caught_and_unrelated_is_not(
        self, store, pg_backend, throwaway_agent
    ) -> None:
        before_count = _live_memories_count()
        print(f"\n[live-dedup] memories count BEFORE test writes: {before_count}")

        # 1) Seed a few distinct real memories via the real store + real
        # pgvector backend (real embeddings hit mxbai over the network).
        seeded = []
        seeded.append(
            store.snapshot(
                title="mxbai embed server location",
                content="The mxbai embedding server runs on 192.168.0.100 port 11434.",
                source="live-dedup-test",
            )
        )
        seeded.append(
            store.snapshot(
                title="skmem-pg location",
                content="skmem-pg (the Postgres+pgvector container) listens on localhost port 5432.",
                source="live-dedup-test",
            )
        )
        seeded.append(
            store.snapshot(
                title="office thermostat note",
                content="The office thermostat is set to 68 degrees on weekdays.",
                source="live-dedup-test",
            )
        )

        target_id = seeded[0].id
        print(f"[live-dedup] seeded {len(seeded)} memories for agent={throwaway_agent}")
        print(f"[live-dedup] target memory id (mxbai-location): {target_id}")

        # 2) A real paraphrase of memory #1 -- different wording, same fact.
        paraphrase = "The mxbai-embed-large server is hosted at .100:11434."
        paraphrase_matches = store.check_duplicate(paraphrase)
        paraphrase_ids = [m["id"] for m in paraphrase_matches]
        # Also fetch the raw (unfiltered) candidates so we can print/inspect
        # the actual similarity even if filtering changes the returned set.
        raw_paraphrase_candidates = pg_backend.find_similar(paraphrase, k=5)
        target_sim = next(
            (c["similarity"] for c in raw_paraphrase_candidates if c["id"] == target_id),
            None,
        )
        print(f"[live-dedup] paraphrase candidates (raw): {raw_paraphrase_candidates}")
        print(f"[live-dedup] paraphrase -> target similarity: {target_sim}")
        print(f"[live-dedup] check_duplicate(paraphrase) matched ids: {paraphrase_ids}")

        assert target_sim is not None, "target memory did not come back as a candidate at all"
        assert target_sim >= DEFAULT_THRESHOLD, (
            f"paraphrase similarity {target_sim} fell below the tuned default "
            f"threshold {DEFAULT_THRESHOLD} -- dedup would miss a real near-duplicate"
        )
        assert target_id in paraphrase_ids, (
            "store.check_duplicate() with the DEFAULT threshold did not surface "
            "the near-duplicate memory"
        )

        # 3) Unrelated content -- must NOT be flagged as a duplicate of
        # anything we seeded.
        unrelated = "Zatarain's jambalaya is made with chicken thighs and three peppers."
        unrelated_matches = store.check_duplicate(unrelated)
        raw_unrelated_candidates = pg_backend.find_similar(unrelated, k=5)
        top_unrelated_sim = (
            max((c["similarity"] for c in raw_unrelated_candidates), default=0.0)
        )
        print(f"[live-dedup] unrelated candidates (raw): {raw_unrelated_candidates}")
        print(f"[live-dedup] unrelated -> top candidate similarity: {top_unrelated_sim}")
        print(f"[live-dedup] check_duplicate(unrelated) matched ids: {[m['id'] for m in unrelated_matches]}")

        assert top_unrelated_sim < DEFAULT_THRESHOLD, (
            f"unrelated content's top similarity {top_unrelated_sim} cleared the "
            f"default threshold {DEFAULT_THRESHOLD} -- would be a false-positive dedup hit"
        )
        assert unrelated_matches == [], (
            "store.check_duplicate() with the DEFAULT threshold wrongly flagged "
            "unrelated content as a near-duplicate of a seeded memory"
        )

        after_count = _live_memories_count()
        print(f"[live-dedup] memories count AFTER test writes (pre-teardown): {after_count}")
        assert after_count == before_count + len(seeded), (
            "expected exactly the seeded rows to have been added for the throwaway agent"
        )
