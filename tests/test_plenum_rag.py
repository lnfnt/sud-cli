# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Cement-тесты локального RAG-поиска по корпусу пленумов (sud plenum search)."""

import json
import os
import sqlite3
import struct

import pytest

from sud_cli.plenum_rag import PlenumCorpusError, search_plenum


def _build_corpus(tmp_path):
    db = tmp_path / "plenum-corpus.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE documents (
            sha256 TEXT PRIMARY KEY, canonical_name TEXT, court TEXT, doc_type TEXT,
            date TEXT, number TEXT, title TEXT, status TEXT, category TEXT, ocr TEXT,
            superseded_by TEXT
        );
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY, doc_sha TEXT, point TEXT, heading TEXT,
            text TEXT, char_count INTEGER, structure TEXT, status TEXT, superseded_by TEXT
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(text);
        CREATE TABLE chunk_embeddings (
            chunk_id TEXT PRIMARY KEY, embedding BLOB, dim INTEGER, updated_at TEXT
        );
        """
    )
    docs = [
        ("sha-active", "VS_PP_active.pdf.md", "active"),
        ("sha-overturned", "VS_PP_overturned.pdf.md", "overturned"),
    ]
    conn.executemany(
        "INSERT INTO documents (sha256, canonical_name, court, doc_type, date, number,"
        " title, status, category, ocr, superseded_by) VALUES (?,?,NULL,NULL,NULL,NULL,"
        "NULL,?,NULL,NULL,NULL)",
        docs,
    )
    chunks = [
        ("cid-1", "sha-active", "1", "Пункт 1", "Неустойка снижается судом по статье 333 ГК РФ."),
        ("cid-2", "sha-overturned", "5", "Пункт 5",
         "Уникальныйпереброс неустойка по отмененному пункту пять."),
    ]
    conn.executemany(
        "INSERT INTO chunks (chunk_id, doc_sha, point, heading, text, char_count,"
        " structure, status, superseded_by) VALUES (?,?,?,?,?,0,NULL,'active',NULL)",
        chunks,
    )
    conn.execute(
        "INSERT INTO chunks_fts (rowid, text) SELECT rowid, text FROM chunks"
    )
    vec = struct.pack("4f", 1.0, 0.0, 0.0, 0.0)
    conn.execute(
        "INSERT INTO chunk_embeddings (chunk_id, embedding, dim, updated_at)"
        " VALUES ('cid-1', ?, 4, 'now')",
        (vec,),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def corpus_db(tmp_path):
    return _build_corpus(tmp_path)


def test_missing_db_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SUD_PLENUM_DB", str(tmp_path / "absent.db"))
    monkeypatch.delenv("SUD_PLENUM_EMBED_URL", raising=False)
    with pytest.raises(PlenumCorpusError):
        search_plenum("неустойка")


def test_active_only_filter(corpus_db, monkeypatch):
    monkeypatch.setenv("SUD_PLENUM_DB", str(corpus_db))
    monkeypatch.setenv("SUD_PLENUM_EMBED_URL", "http://127.0.0.1:1/unreachable")
    results, meta = search_plenum("Уникальныйпереброс", limit=5)
    # пункт отменённого акта физически не попадает в выдачу
    assert results == []
    assert meta["scope"] == "active-only"


def test_vector_unavailable_degrades_honestly(corpus_db, monkeypatch):
    monkeypatch.setenv("SUD_PLENUM_DB", str(corpus_db))
    monkeypatch.setenv("SUD_PLENUM_EMBED_URL", "http://127.0.0.1:1/unreachable")
    results, meta = search_plenum("неустойка", limit=5)
    assert meta["vector"] == "unavailable"
    assert meta["fts_hits"] >= 1
    assert any("333" in r["text"] for r in results)


def test_vector_path_with_stub(corpus_db, monkeypatch):
    monkeypatch.setenv("SUD_PLENUM_DB", str(corpus_db))
    import sud_cli.plenum_rag as rag

    monkeypatch.setattr(rag, "_query_embedding", lambda url, q: [1.0, 0.0, 0.0, 0.0])
    results, meta = search_plenum("неустойка", limit=5)
    assert meta["vector"] == "ok"
    assert meta["vector_hits"] == 1
    assert results[0]["chunk_id"] == "cid-1"


def test_cli_json_smoke(corpus_db, monkeypatch, capsys):
    monkeypatch.setenv("SUD_PLENUM_DB", str(corpus_db))
    monkeypatch.setenv("SUD_PLENUM_EMBED_URL", "http://127.0.0.1:1/unreachable")
    from sud_cli.cli import main

    rc = main(["plenum", "search", "неустойка", "--limit", "2", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["meta"]["command"] == "plenum search"
    assert payload["meta"]["scope"] == "active-only"
