"""A corrected flat file never reached pg, and nothing noticed.

The backfill is `INSERT ... ON CONFLICT (id) DO NOTHING`, so it only ever
inserts ids that are ABSENT. An id present in both flat and pg is never
revisited. So a flat file corrected after the fact (repair.py backfilling a
title, an operator fixing content, a Syncthing update from another node) left
pg permanently stale, and `drift_check` compares id SETS only, so
sqlite_only/flat_only/pg_only all read zero the whole time.

The embedding is the worst of it: computed from the old text and NOT null, so
the null-embedding pass never touches it either. pg keeps answering semantic
queries from text that no longer exists on disk.

Found 2026-10-20 after repairing 35 rows by hand: the repair fixed the flat
files, every drift count stayed 0, and pg kept serving the old content.

Detection is always on. The UPDATE is opt-in, because a flat file is not
unconditionally better than the row it would replace, and a stub-flagged
recovery preview must never overwrite fuller content already in pg.
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from skmemory import reconcile as reconcile_mod


class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


class _FakePsql:
    """Fake psql that also answers the content-hash query.

    ``pg_content`` maps id -> the content string pg currently holds, which is
    what lets a test make the flat file agree or disagree with pg.
    """

    def __init__(self, pg_content: dict[str, str], pg_title=None, pg_broken=()):
        self.pg_content = dict(pg_content)
        #: What pg holds in its TITLE column per id. Defaults to the same title
        #: the flat file uses, so a content-only test stays a content-only test.
        self.pg_title = dict(pg_title or {})
        #: Ids whose memory_json is missing a required key (structurally
        #: broken), which is stale regardless of the hashes.
        self.pg_broken = set(pg_broken)
        self.statements: list[str] = []
        self.copy_inputs: list[str] = []
        self.updated = False

    @property
    def pg_ids(self):
        return list(self.pg_content)

    def run(self, args, capture_output=False, text=False, input=None, timeout=None):
        if args and args[0] == "sk-alert":
            return _CP(stdout="")
        sql = ""
        if "-c" in args:
            sql = args[args.index("-c") + 1]
        elif "-f" in args:
            sql = input or ""
        self.statements.append(sql)
        low = sql.lower()

        if "copy" in low and "from stdin" in low:
            if "format csv" in low:
                self.copy_inputs.append(input or "")
            return _CP(stdout="")
        if "on conflict (id) do update" in low:
            self.updated = True
            return _CP(stdout="")
        if "md5(" in low:
            ids = re.findall(r"'([^']+)'", sql.split(" in (")[-1]) if " in (" in sql else []
            out = []
            for mid in ids:
                if mid in self.pg_content:
                    # Mirror the real query: md5(title || US || content), plus
                    # an ok/broken flag.
                    title = self.pg_title.get(mid, "t")
                    payload = title + "\x1f" + self.pg_content[mid]
                    digest = hashlib.md5(payload.encode("utf-8")).hexdigest()
                    flag = "broken" if mid in self.pg_broken else "ok"
                    out.append(f"{mid} {digest} {flag}")
            return _CP(stdout="\n".join(out) + ("\n" if out else ""))
        if "delete from memories" in low:
            return _CP(stdout="0\n")
        if low.startswith("select id from memories where agent"):
            return _CP(stdout="\n".join(self.pg_ids) + ("\n" if self.pg_ids else ""))
        if low.startswith("select id from memories;"):
            return _CP(stdout="\n".join(self.pg_ids) + ("\n" if self.pg_ids else ""))
        if "embedding is null" in low:
            return _CP(stdout="")
        if "count(*) filter" in low:
            n = len(self.pg_ids)
            return _CP(stdout=f"{n}/{n}")
        return _CP(stdout="")


def _write_flat(mem_dir, mem_id, content, *, layer="short-term", stub=False):
    d = mem_dir / layer
    d.mkdir(parents=True, exist_ok=True)
    payload = {"id": mem_id, "content": content, "layer": layer, "title": "t"}
    if stub:
        payload["metadata"] = {"recovered_from_sqlite_preview": True}
    (d / f"{mem_id}.json").write_text(json.dumps(payload))
    return mem_id


@pytest.fixture
def fake_embed(monkeypatch):
    calls = []

    def _post(url, json=None, timeout=None):  # noqa: A002 - matches requests.post
        calls.append(json)

        class _Resp:
            def json(self):
                n = len(json["input"]) if json and "input" in json else 1
                return {"embeddings": [[0.1, 0.2] for _ in range(n)]}

        return _Resp()

    monkeypatch.setattr(reconcile_mod.requests, "post", _post)
    return calls


def _run(monkeypatch, fake, mem, **kw):
    monkeypatch.setattr(reconcile_mod.subprocess, "run", fake.run)
    return reconcile_mod.reconcile(
        "test-agent", mem_dir=str(mem), psql_cmd=["psql"], verbose=False, **kw
    )


# --------------------------------------------------------------------------- #
# Detection (always on)
# --------------------------------------------------------------------------- #


class TestDetection:
    def test_a_changed_flat_file_is_reported_as_stale(self, monkeypatch, tmp_path, fake_embed):
        """The signal that did not exist. pg has the OLD content."""
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "corrected content")
        fake = _FakePsql({"m1": "stale content"})

        stats = _run(monkeypatch, fake, mem)

        assert stats["stale"] == 1
        assert stats["stale_ids"] == ["m1"]
        assert stats["refreshed"] == 0, "detection must not mutate"
        assert fake.updated is False

    def test_matching_content_is_not_stale(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "same content")
        fake = _FakePsql({"m1": "same content"})

        stats = _run(monkeypatch, fake, mem)

        assert stats["stale"] == 0
        assert stats["stale_ids"] == []

    def test_detection_does_not_call_the_embed_endpoint(self, monkeypatch, tmp_path, fake_embed):
        """A read-only check must not cost an embed."""
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "corrected content")
        fake = _FakePsql({"m1": "stale content"})

        _run(monkeypatch, fake, mem)

        assert fake_embed == []

    def test_only_the_changed_id_is_flagged(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flat(mem, "same", "unchanged")
        _write_flat(mem, "diff", "now different")
        fake = _FakePsql({"same": "unchanged", "diff": "was this"})

        stats = _run(monkeypatch, fake, mem)

        assert stats["stale"] == 1
        assert stats["stale_ids"] == ["diff"]


# --------------------------------------------------------------------------- #
# Refresh (opt-in)
# --------------------------------------------------------------------------- #


class TestRefresh:
    def test_refresh_updates_the_stale_row(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "corrected content")
        fake = _FakePsql({"m1": "stale content"})

        stats = _run(monkeypatch, fake, mem, refresh=True)

        assert stats["stale"] == 1
        assert stats["refreshed"] == 1
        assert fake.updated is True, "must be ON CONFLICT DO UPDATE, not DO NOTHING"
        assert any("corrected content" in payload for payload in fake.copy_inputs)

    def test_refresh_recomputes_the_embedding(self, monkeypatch, tmp_path, fake_embed):
        """The old vector is present and WRONG, so a null-embedding pass cannot help."""
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "corrected content")
        fake = _FakePsql({"m1": "stale content"})

        _run(monkeypatch, fake, mem, refresh=True)

        assert fake_embed, "content changed and the embedding was not recomputed"
        assert any("corrected content" in str(c.get("input")) for c in fake_embed)

    def test_dry_run_refresh_writes_nothing(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "corrected content")
        fake = _FakePsql({"m1": "stale content"})

        stats = _run(monkeypatch, fake, mem, refresh=True, dry_run=True)

        assert stats["stale"] == 1
        assert stats["refreshed"] == 0
        assert fake.updated is False
        assert fake_embed == []

    def test_nothing_stale_means_no_update_statement(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "same content")
        fake = _FakePsql({"m1": "same content"})

        stats = _run(monkeypatch, fake, mem, refresh=True)

        assert stats["refreshed"] == 0
        assert fake.updated is False


# --------------------------------------------------------------------------- #
# The stub guard applies here too
# --------------------------------------------------------------------------- #


class TestStubNeverRefreshes:
    def test_a_stub_flat_file_never_overwrites_pg(self, monkeypatch, tmp_path, fake_embed):
        """The exact situation found on one node: 727 of 888 flat files were
        lossy `recovered_from_sqlite_preview` previews while pg still held the
        fuller content. Refreshing from those would have destroyed it.
        """
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "[recovered from SQLite preview", stub=True)
        fake = _FakePsql({"m1": "the full original content, much longer"})

        stats = _run(monkeypatch, fake, mem, refresh=True)

        assert stats["stale"] == 0, "a stub must not even be counted as refreshable"
        assert stats["refreshed"] == 0
        assert fake.updated is False
        assert fake_embed == []

    def test_force_stubs_does_not_unlock_refresh(self, monkeypatch, tmp_path, fake_embed):
        """force_stubs is about BACKFILLING an absent id, not overwriting a
        present row. Those are different risks and it must not grant both.
        """
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "[recovered from SQLite preview", stub=True)
        fake = _FakePsql({"m1": "the full original content"})

        stats = _run(monkeypatch, fake, mem, refresh=True, force_stubs=True)

        assert stats["refreshed"] == 0
        assert fake.updated is False


# --------------------------------------------------------------------------- #
# Failure honesty
# --------------------------------------------------------------------------- #


def test_an_unreadable_hash_query_is_not_reported_as_clean(monkeypatch, tmp_path, fake_embed):
    """Undetectable is not "no divergence". Same rule as pg_only: None/0 must
    not be produced by a check that could not run.
    """
    mem = tmp_path / "memory"
    _write_flat(mem, "m1", "corrected content")

    class _Broken(_FakePsql):
        def run(self, args, **kw):
            sql = args[args.index("-c") + 1] if "-c" in args else (kw.get("input") or "")
            if "md5(" in sql.lower():
                raise RuntimeError("transport died mid-check")
            return super().run(args, **kw)

    fake = _Broken({"m1": "stale content"})
    stats = _run(monkeypatch, fake, mem, refresh=True)

    # It could not tell, so it must not claim a refresh happened.
    assert stats["refreshed"] == 0
    assert fake.updated is False


class TestTitleOnlyDivergence:
    """The case that motivated this phase and that it originally MISSED.

    `repair.py` backfills a missing title and leaves content untouched. The
    first version of this detector hashed CONTENT alone, so the hashes matched
    and the row read as fresh. Found on a second node: 10 ava rows had no title
    in pg, a title in the flat file, byte-identical content, and the detector
    reported STALE=0 while health reported 10 unparseable rows.
    """

    def test_a_title_only_change_is_stale(self, monkeypatch, tmp_path, fake_embed):
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "identical content")  # flat title is "t"
        fake = _FakePsql({"m1": "identical content"}, pg_title={"m1": ""})

        stats = _run(monkeypatch, fake, mem)

        assert stats["stale"] == 1, "a title-only correction must be detected"
        assert stats["stale_ids"] == ["m1"]

    def test_a_structurally_broken_row_is_stale_even_if_hashes_agree(
        self, monkeypatch, tmp_path, fake_embed
    ):
        """memory_json missing a required key is stale by definition."""
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "identical content")
        fake = _FakePsql({"m1": "identical content"}, pg_broken={"m1"})

        stats = _run(monkeypatch, fake, mem)

        assert stats["stale"] == 1

    def test_a_broken_row_with_an_UNLOADABLE_flat_file_is_not_refreshable(
        self, monkeypatch, tmp_path, fake_embed
    ):
        """Refreshing from a flat file that is itself broken fixes nothing."""
        mem = tmp_path / "memory"
        d = mem / "short-term"
        d.mkdir(parents=True, exist_ok=True)
        (d / "m1.json").write_text(json.dumps({"id": "m1", "content": "identical content"}))
        fake = _FakePsql({"m1": "identical content"}, pg_title={"m1": ""}, pg_broken={"m1"})

        stats = _run(monkeypatch, fake, mem)

        assert stats["stale"] == 0

    def test_an_identical_row_is_still_not_stale(self, monkeypatch, tmp_path, fake_embed):
        """The new checks must not make everything look stale."""
        mem = tmp_path / "memory"
        _write_flat(mem, "m1", "identical content")
        fake = _FakePsql({"m1": "identical content"})  # title defaults to "t"

        stats = _run(monkeypatch, fake, mem)

        assert stats["stale"] == 0
