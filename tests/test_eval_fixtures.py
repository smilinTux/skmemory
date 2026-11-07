"""Schema, referential-integrity and privacy checks for the v1 eval fixtures."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from skmemory.eval import fixtures as fx

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
DSN = re.compile(r"postgres(ql)?://", re.I)
TAILNET_IP = re.compile(r"(?<![\d.])100\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])")


def test_all_loaders_return_models():
    assert len(fx.load_corpus()) == 59
    assert len(fx.load_queries()) == 53
    assert len(fx.load_intentions()) == 12
    assert len(fx.load_intention_transitions()) == 16
    assert len(fx.load_prompts()) == 17
    assert len(fx.load_outcomes()) == 34
    assert len(fx.load_outcomes_expected().expected) == 4
    assert len(fx.load_supersession().chains) == 4
    assert len(fx.load_decay().items) == 5


def test_ids_unique():
    ids = [m.id for m in fx.load_corpus()]
    assert len(ids) == len(set(ids))


def test_gold_and_must_not_resolve_to_corpus():
    corpus = {m.id for m in fx.load_corpus()}
    for q in fx.load_queries():
        for i in q.gold_ids + q.must_not_ids:
            assert i in corpus, f"{q.id}: {i} not in corpus"
        assert not set(q.gold_ids) & set(q.must_not_ids), q.id


def test_other_fixture_references_resolve():
    corpus = {m.id for m in fx.load_corpus()}
    for m in fx.load_corpus():
        if m.supersedes_id:
            assert m.supersedes_id in corpus
    for i in fx.load_intentions():
        if i.origin_memory_id:
            assert i.origin_memory_id in corpus
    sup = fx.load_supersession()
    for chain in sup.chains:
        for i in chain.ids:
            assert i in corpus
        for c in chain.as_of_checks:
            assert set(c.valid) <= set(chain.ids)
    for c in sup.write_time_conflict_cases:
        assert c.existing in corpus
    intentions = {i.id for i in fx.load_intentions()}
    for p in fx.load_prompts():
        if p.id != "p-16":
            assert set(p.expected_fire) <= intentions


def test_supersession_as_of_checks_match_validity_windows():
    corpus = {m.id: m for m in fx.load_corpus()}
    for chain in fx.load_supersession().chains:
        for check in chain.as_of_checks:
            valid = [
                i
                for i in chain.ids
                if corpus[i].valid_from <= check.as_of
                and (corpus[i].valid_to is None or check.as_of < corpus[i].valid_to)
            ]
            assert valid == check.valid


def test_outcomes_expected_ranks_are_a_permutation():
    exp = fx.load_outcomes_expected().expected
    assert sorted(e.rank for e in exp.values()) == list(range(1, len(exp) + 1))


def test_every_row_is_synthetic():
    base = fx.FIXTURE_DIR
    files = [p for p in base.iterdir() if p.suffix in {".jsonl", ".json", ".md"}]
    assert len(files) >= 10
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not EMAIL.search(line), f"{path.name}:{n} email"
            assert not DSN.search(line), f"{path.name}:{n} DSN"
            assert not TAILNET_IP.search(line), f"{path.name}:{n} 100.x address"


def test_privacy_scanner_catches_offenders():
    assert EMAIL.search("mail me at a.b@example.com")
    assert not EMAIL.search('"context_tag": "@public"')
    assert not EMAIL.search("recover-schema@1.2")
    assert DSN.search("postgresql://u:p@h/db")
    assert TAILNET_IP.search("host 100.64.1.2 up")
    assert not TAILNET_IP.search("version 1100.64.1.2x1")


def test_unknown_field_is_rejected(tmp_path):
    (tmp_path / "corpus-v1.jsonl").write_text('{"id": "x", "bogus": 1}\n')
    with pytest.raises(ValidationError):
        fx.load_corpus(tmp_path)
