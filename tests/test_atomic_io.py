"""Tests for the durable atomic flat-file write primitive.

Regression coverage for the 2026-10-16 SQLite-only orphan incident: a plain
``Path.write_text`` only guarantees bytes reach the OS page cache, not the
disk. ``atomic_write_bytes``/``atomic_write_text`` must (a) never leave a
partial file behind on failure, (b) leave any prior content untouched when a
write fails, and (c) actually call fsync so the write is durable.
"""

from __future__ import annotations

import os

import pytest

from skmemory.atomic_io import atomic_write_bytes, atomic_write_text


class TestAtomicWriteBytes:
    def test_writes_full_content(self, tmp_path):
        target = tmp_path / "memory.json"
        atomic_write_bytes(target, b'{"hello": "world"}')
        assert target.read_bytes() == b'{"hello": "world"}'

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "short-term" / "abc.json"
        atomic_write_bytes(target, b"data")
        assert target.exists()
        assert target.read_bytes() == b"data"

    def test_no_tmp_file_left_behind_on_success(self, tmp_path):
        target = tmp_path / "memory.json"
        atomic_write_bytes(target, b"data")
        leftovers = [p for p in tmp_path.iterdir() if p.name != "memory.json"]
        assert leftovers == []

    def test_calls_fsync_on_file_and_directory(self, tmp_path, monkeypatch):
        target = tmp_path / "memory.json"
        calls = []
        real_fsync = os.fsync

        def spy_fsync(fd):
            calls.append(fd)
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", spy_fsync)
        atomic_write_bytes(target, b"data")
        # At least one fsync for the file; directory fsync is best-effort.
        assert len(calls) >= 1

    def test_failed_write_raises_and_leaves_no_tmp_file(self, tmp_path, monkeypatch):
        target = tmp_path / "memory.json"

        def boom(fd):
            raise OSError("disk full")

        monkeypatch.setattr(os, "fsync", boom)

        with pytest.raises(OSError):
            atomic_write_bytes(target, b"data")

        assert not target.exists()
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []

    def test_failed_write_does_not_clobber_existing_file(self, tmp_path, monkeypatch):
        target = tmp_path / "memory.json"
        atomic_write_bytes(target, b"original")

        def boom(fd):
            raise OSError("disk full")

        monkeypatch.setattr(os, "fsync", boom)

        with pytest.raises(OSError):
            atomic_write_bytes(target, b"new-content-that-should-never-land")

        assert target.read_bytes() == b"original"


class TestAtomicWriteText:
    def test_writes_text_as_utf8(self, tmp_path):
        target = tmp_path / "memory.json"
        atomic_write_text(target, '{"title": "café"}')
        assert target.read_text(encoding="utf-8") == '{"title": "café"}'
