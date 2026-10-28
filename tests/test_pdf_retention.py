"""Tests for the budget-bounded PDF original retention store."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from mathbank.pdf_retention import PdfRetentionStore, retention_settings_from_env


@pytest.fixture
def store(tmp_path):
    return PdfRetentionStore(tmp_path / "retained", max_total_bytes=10 * 1024,
                             max_age_days=365)


PDF_A = b"%PDF-1.4 alpha paper"
PDF_B = b"%PDF-1.4 beta paper"


def test_save_deduplicates_by_sha(store):
    first = store.save(PDF_A, "a.pdf")
    second = store.save(PDF_A, "a-renamed.pdf")
    assert first["sha256"] == second["sha256"]
    assert second["imports"] == 2
    # The rename on a duplicate import is cosmetic, not a new entry.
    assert len(store.entries()) == 1
    assert store.usage()["file_count"] == 1
    assert store.usage()["total_bytes"] == len(PDF_A)


def test_open_returns_bytes_and_touches_lru(store):
    entry = store.save(PDF_A, "a.pdf")
    assert store.open(entry["sha256"]) == PDF_A
    assert store.open(entry["sha256"].upper()) is None  # uppercase sha rejected
    assert store.open("0" * 64) is None


def test_budget_eviction_removes_oldest_entry(tmp_path):
    PDF_C = b"%PDF-1.4 gamma paper"
    # Budget is one byte short of A+B+C; saving C with B stale evicts B.
    store = PdfRetentionStore(tmp_path / "r",
                              max_total_bytes=len(PDF_A) + len(PDF_B) + len(PDF_C) - 1,
                              max_age_days=0)
    a = store.save(PDF_A, "a.pdf")
    b_entry = store.save(PDF_B, "b.pdf")
    store.open(a["sha256"])  # a is now the most recently used
    store.save(PDF_C, "c.pdf")
    remaining = {entry["sha256"] for entry in store.entries()}
    assert a["sha256"] in remaining
    assert b_entry["sha256"] not in remaining
    assert store.usage()["total_bytes"] == len(PDF_A) + len(PDF_C)


def test_pinned_entries_survive_budget_eviction(tmp_path):
    PDF_C = b"%PDF-1.4 gamma paper"
    PDF_D = b"%PDF-1.4 delta papers!"
    store = PdfRetentionStore(tmp_path / "r",
                              max_total_bytes=len(PDF_A) + len(PDF_B) + len(PDF_C) - 1,
                              max_age_days=0)
    a = store.save(PDF_A, "a.pdf")
    store.set_pinned(a["sha256"], True)
    b_entry = store.save(PDF_B, "b.pdf")
    store.open(a["sha256"])
    store.save(PDF_C, "c.pdf")  # LRU is b; a is pinned and survives.
    remaining = {entry["sha256"] for entry in store.entries()}
    assert a["sha256"] in remaining
    assert b_entry["sha256"] not in remaining
    # Unpinning lets the next over-budget save reclaim it: a is then the LRU.
    store.set_pinned(a["sha256"], False)
    store.save(PDF_D, "d.pdf")
    final = {entry["sha256"] for entry in store.entries()}
    assert a["sha256"] not in final
    assert len(final) >= 2


def test_age_eviction_uses_last_access(tmp_path):
    store = PdfRetentionStore(tmp_path / "r", max_total_bytes=None, max_age_days=30)
    entry = store.save(PDF_A, "a.pdf")
    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).isoformat(timespec="seconds")
    store._entries[entry["sha256"]]["last_accessed_at"] = stale
    evicted = store.enforce()
    assert evicted == 1
    assert store.entries() == []


def test_disabled_store_never_writes(tmp_path):
    store = PdfRetentionStore(tmp_path / "r", max_total_bytes=0, max_age_days=365)
    assert store.enabled is False
    assert store.save(PDF_A, "a.pdf") is None
    assert list(tmp_path.rglob("*.pdf")) == []


def test_index_survives_restart_and_reconciles_files(tmp_path):
    store = PdfRetentionStore(tmp_path / "r", max_total_bytes=None, max_age_days=0)
    entry = store.save(PDF_A, "a.pdf")
    reopened = PdfRetentionStore(tmp_path / "r", max_total_bytes=None, max_age_days=0)
    assert reopened.open(entry["sha256"]) == PDF_A
    # A file present on disk but missing from the index is adopted.
    stray = tmp_path / "r" / "files" / "bb" / ("b" * 64 + ".pdf")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(PDF_B)
    reopened2 = PdfRetentionStore(tmp_path / "r", max_total_bytes=None, max_age_days=0)
    assert reopened2.open("b" * 64) == PDF_B
    assert reopened2.usage()["file_count"] == 2


def test_delete_removes_file_and_index_entry(store):
    entry = store.save(PDF_A, "a.pdf")
    assert store.delete(entry["sha256"]) is True
    assert store.open(entry["sha256"]) is None
    assert store.delete(entry["sha256"]) is False


def test_settings_from_env_defaults_and_override():
    defaults = retention_settings_from_env({})
    assert defaults["max_total_bytes"] == 2048 * 1024 * 1024
    assert defaults["max_age_days"] == 365
    disabled = retention_settings_from_env({"MATHBANK_PDF_RETENTION_MB": "0",
                                            "MATHBANK_PDF_RETENTION_DAYS": "0"})
    assert disabled["max_total_bytes"] == 0
    assert disabled["max_age_days"] == 0
    custom = retention_settings_from_env({"MATHBANK_PDF_RETENTION_MB": "512",
                                          "MATHBANK_PDF_RETENTION_DAYS": "90"})
    assert custom["max_total_bytes"] == 512 * 1024 * 1024
    assert custom["max_age_days"] == 90


def test_register_document_input_retains_pdf(tmp_path, monkeypatch):
    import main
    store = PdfRetentionStore(tmp_path / "r", max_total_bytes=None, max_age_days=365)
    monkeypatch.setattr(main, "PDF_RETENTION", store)
    source = main._register_document_input("task-1", b"%PDF-1.4 wiring", "pdf", "paper.pdf")
    assert source["retained_pdf"]["sha256"] == __import__("hashlib").sha256(b"%PDF-1.4 wiring").hexdigest()
    assert store.usage()["file_count"] == 1
    # Word imports never touch the PDF retention store.
    word_source = main._register_document_input("task-2", b"docx-bytes", "docx", "paper.docx")
    assert "retained_pdf" not in word_source
    assert store.usage()["file_count"] == 1


def test_register_document_input_without_store(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "PDF_RETENTION", None)
    source = main._register_document_input("task-3", b"%PDF-1.4 x", "pdf", "x.pdf")
    assert "retained_pdf" not in source
