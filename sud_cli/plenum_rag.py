# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Локальный RAG-поиск по корпусу постановлений Пленума/Президиума ВС РФ
и обзоров судебной практики (гибрид BM25 + векторный, слияние RRF).

Law Zero: любая деградация — явная (в метаданных ответа), не тихая.

Контракт:
- ищет СТРОГО по действующим пунктам (documents.status='active');
- векторный контур опционален: сервер эмбеддингов недоступен → FTS-only
  с честным флагом vector="unavailable" в meta, а не молчаливый обрез;
- база корпуса отсутствует/битая — честная ошибка, exit != 0;
- query-расширение (PRF) и внутридокументный переранк — опциональны:
  numpy недоступен или эмбеддингов нет → этапы честно помечаются
  в meta и поиск деградирует, а не падает.

Пути без внутренних абсолютов (публичный контур):
- база: env SUD_PLENUM_DB, дефолт ~/.sud-cli/plenum-corpus.db;
- эмбеддер: env SUD_PLENUM_EMBED_URL, дефолт http://127.0.0.1:11434/api/embeddings;
- PRF: env SUD_PLENUM_PRF=0 выключает расширение запроса.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.request
from pathlib import Path

try:  # опциональное ускорение; публичный контур не обязан тащить numpy
    import numpy as _np
except Exception:  # pragma: no cover - зависит от окружения
    _np = None

DEFAULT_DB = Path.home() / ".sud-cli" / "plenum-corpus.db"
DEFAULT_EMBED_URL = "http://127.0.0.1:11434/api/embeddings"
EMBED_MODEL = "bge-m3:latest"
EMBED_TIMEOUT_S = 20
RRF_K = 60
# Глубина кандидатных пулов до слияния RRF. Золотой сет 2026-11-05:
# узкие пулы (20/25) теряли верный чанк до этапа слияния — рост пулов
# дал +2 к точности. С numpy-сканом глубина пула почти бесплатна,
# поэтому векторный пул расширен (промахи W5: верный пункт вне сотни).
CAND_FTS = 60
CAND_VEC = 300
# PRF: сколько сид-чанков читаем и сколько терминов добавляем в запрос.
PRF_SEED = 5
PRF_TERMS = 8
# Внутридокументный переранк: сколько топ-документов углубляем.
RERANK_DOCS = 12
_STOPWORDS = {
    "который", "которые", "которая", "которых", "которым", "которыми",
    "котором", "которого", "которому", "которой", "которую", "которое",
    "которых", "которыми", "которых", "также", "свои", "своих", "своим",
    "своего", "свой", "своя", "свое", "этого", "этом", "эти", "этих",
    "этим", "этой", "эту", "это", "или", "его", "ее", "их", "ими", "нем",
    "ней", "них", "при", "под", "над", "для", "без", "так", "как", "что",
    "где", "когда", "либо", "иначе", "тогда", "суд", "суда", "суде",
    "судом", "суды", "дела", "деле", "делу", "дел", "решения", "решение",
    "решении", "пункт", "пункта", "пункте", "настоящего", "постановления",
    "постановление", "постановлением", "верховного", "верховный", "суда",
    "федерации", "статьи", "статья", "статьей", "части", "часть", "частью",
    "кодекса", "закона", "законодательства", "соответствии", "случае",
    "случаи", "является", "являются", "порядке", "вопросы", "вопросов",
    "вопросам", "отношении", "отношений", "которых", "котором",
    "законодательством", "практике", "практики", "российской",
}


class PlenumCorpusError(RuntimeError):
    """База корпуса отсутствует, битая или не та схема."""


def corpus_db_path() -> Path:
    env = (os.environ.get("SUD_PLENUM_DB") or "").strip()
    return Path(env).expanduser() if env else DEFAULT_DB


def _open_corpus(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise PlenumCorpusError(
            f"корпус пленумов не найден: {path} "
            f"(задайте SUD_PLENUM_DB либо положите базу по пути по умолчанию)"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    need = {"documents", "chunks", "chunks_fts"}
    have = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
    }
    missing = need - have
    if missing:
        conn.close()
        raise PlenumCorpusError(f"корпус {path} не той схемы, нет таблиц: {sorted(missing)}")
    return conn


def _query_embedding(embed_url: str, query: str):
    payload = json.dumps({"model": EMBED_MODEL, "prompt": query}).encode("utf-8")
    req = urllib.request.Request(
        embed_url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    emb = data.get("embedding")
    if not emb:
        return None
    return [float(x) for x in emb]


def _cosine(v1, v2) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if not n1 or not n2:
        return 0.0
    return dot / (n1 * n2)


def _bm25(conn: sqlite3.Connection, query: str, limit: int = 25):
    words = [w for w in re.findall(r"[а-яёa-z0-9]+", query.lower()) if len(w) > 2]
    fts_query = " OR ".join(words) if words else query
    return conn.execute(
        """
        SELECT c.chunk_id, d.canonical_name, d.date, d.number, c.point, c.heading, c.text, rank
        FROM chunks_fts f
        JOIN chunks c ON f.rowid = c.rowid
        JOIN documents d ON c.doc_sha = d.sha256
        WHERE chunks_fts MATCH ? AND d.status = 'active' AND c.status = 'active'
        ORDER BY rank
        LIMIT ?
        """,
        (fts_query, limit),
    ).fetchall()


def _vector_rows(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT e.chunk_id, e.embedding, d.canonical_name, d.date, d.number,
               c.point, c.heading, c.text
        FROM chunk_embeddings e
        JOIN chunks c ON e.chunk_id = c.chunk_id
        JOIN documents d ON c.doc_sha = d.sha256
        WHERE d.status = 'active' AND c.status = 'active'
        """
    ).fetchall()


def _vector(conn: sqlite3.Connection, q_emb, limit: int = 20):
    rows = _vector_rows(conn)
    scored = []
    for cid, blob, cname, date, num, pt, heading, text in rows:
        vec = struct.unpack(f"{len(blob) // 4}f", blob)
        scored.append((_cosine(q_emb, vec), cid, cname, date, num, pt, heading, text))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


def _vector_np(conn: sqlite3.Connection, q_emb, limit: int):
    """Numpy-скан всех активных эмбеддингов: полная сортировка за ~0.5 c."""
    np = _np
    if np is None:  # вызов без numpy — честный fallback, не падение
        return _vector(conn, q_emb, limit)
    rows = _vector_rows(conn)
    if not rows:
        return []
    dim = len(q_emb)
    mat = np.array(
        [np.frombuffer(blob, dtype=np.float32) for _, blob, *_ in rows],
        dtype=np.float32,
    )
    if mat.shape[1] != dim:  # схема битая/несовместимая — не молчим
        return []
    q = np.array(q_emb, dtype=np.float32)
    mat_norm = mat / np.linalg.norm(mat, axis=1, keepdims=True)
    q_norm = q / np.linalg.norm(q)
    sims = mat_norm @ q_norm
    top_idx = np.argsort(-sims)[:limit]
    scored = []
    for i in top_idx:
        cid, _, cname, date, num, pt, heading, text = rows[i]
        scored.append((float(sims[i]), cid, cname, date, num, pt, heading, text))
    return scored


def _terms(texts) -> list:
    """Значимые термины PRF: частотные слова сид-чанков без стоп-слов."""
    freq: dict = {}
    for t in texts:
        for w in re.findall(r"[а-яё]+", t.lower()):
            if len(w) < 5 or w in _STOPWORDS:
                continue
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:PRF_TERMS]]


def _doc_rerank(conn, q_emb, docs, rrf_by_cid, docs_map):
    """Внутридокументный переранк: для каждого топ-документа выбираем
    лучший по косинусу чанк (верный пункт перестаёт тонуть внутри акта)."""
    picked = []
    if q_emb is None or _np is None:
        for cname in docs:
            cids = [cid for cid, m in docs_map.items() if m[0] == cname]
            best = max(cids, key=lambda c: rrf_by_cid.get(c, 0.0))
            picked.append((cname, best))
        return picked
    for cname in docs:
        rows = conn.execute(
            """
            SELECT e.chunk_id, e.embedding
            FROM chunk_embeddings e
            JOIN chunks c ON e.chunk_id = c.chunk_id
            JOIN documents d ON c.doc_sha = d.sha256
            WHERE d.canonical_name = ? AND d.status = 'active'
              AND c.status = 'active'
            """,
            (cname,),
        ).fetchall()
        if not rows:
            cids = [cid for cid, m in docs_map.items() if m[0] == cname]
            best = max(cids, key=lambda c: rrf_by_cid.get(c, 0.0))
            picked.append((cname, best))
            continue
        np = _np
        q = np.array(q_emb, dtype=np.float32)
        q = q / np.linalg.norm(q)
        # смешанный скоринг: чистый косинус вытеснял эталонный пункт
        # в пользу семантически близкого соседа (регресс v2/запрос 22),
        # поэтому выбор чанка = 0.7*cos + 0.3*rrf-вклад в документ.
        # Косинус доминирует: RRF-только вариант терял пункты 3/4/11.
        doc_rrf_total = sum(
            rrf_by_cid.get(cid, 0.0)
            for cid, m in docs_map.items()
            if m[0] == cname
        ) or 1.0
        best_cid, best_score = None, -1.0
        for cid, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            if v.shape[0] != q.shape[0]:
                continue
            cos = float((v / np.linalg.norm(v)) @ q)
            blend = 0.7 * cos + 0.3 * (rrf_by_cid.get(cid, 0.0) / doc_rrf_total)
            if blend > best_score:
                best_score, best_cid = blend, cid
        if best_cid:
            picked.append((cname, best_cid))
    return picked


def search_plenum(query: str, limit: int = 5, db_path: Path | None = None):
    """Гибридный поиск. Возвращает (results, meta). Fail-closed по корпусу."""
    t0 = time.monotonic()
    path = db_path or corpus_db_path()
    conn = _open_corpus(path)
    try:
        fts_rows = _bm25(conn, query, limit=CAND_FTS)
        embed_url = (os.environ.get("SUD_PLENUM_EMBED_URL") or DEFAULT_EMBED_URL).strip()
        vec_state = "ok"
        prf_state = "off"
        rerank_state = "off"
        vec_rows = []
        q_emb = None
        try:
            q_emb = _query_embedding(embed_url, query)
            if q_emb:
                if _np is not None:
                    vec_rows = _vector_np(conn, q_emb, limit=CAND_VEC)
                else:
                    vec_rows = _vector(conn, q_emb, limit=100)
            else:
                vec_state = "empty_response"
        except Exception:
            vec_state = "unavailable"

        # PRF: расширение запроса терминами сид-чанков (только если вектор жив)
        if (
            q_emb is not None
            and vec_rows
            and os.environ.get("SUD_PLENUM_PRF", "1") != "0"
        ):
            seeds = [r[7] for r in vec_rows[:PRF_SEED]]
            extra = _terms(seeds)
            if extra:
                expanded = query + " " + " ".join(extra)
                prf_rows = _bm25(conn, expanded, limit=CAND_FTS)
                if prf_rows:
                    prf_state = "ok"
                    fts_rows = fts_rows + [r for r in prf_rows if r[0] not in {x[0] for x in fts_rows}]
                else:
                    prf_state = "no_hits"
            else:
                prf_state = "no_terms"

        rrf: dict = {}
        docs_map: dict = {}

        def _put(cid, score_add, cname, date, num, pt, heading, text):
            rrf[cid] = rrf.get(cid, 0.0) + score_add
            docs_map[cid] = (cname, date, num, pt, heading, text)

        for idx, r in enumerate(fts_rows):
            _put(r[0], 1.0 / (RRF_K + idx + 1), r[1], r[2], r[3], r[4], r[5], r[6])
        for idx, r in enumerate(vec_rows):
            _put(r[1], 1.0 / (RRF_K + idx + 1), r[2], r[3], r[4], r[5], r[6], r[7])

        # Топ-документы по сумме RRF своих чанков
        doc_score: dict = {}
        for cid, sc in rrf.items():
            cname = docs_map[cid][0]
            doc_score[cname] = doc_score.get(cname, 0.0) + sc
        top_docs = [d for d, _ in sorted(doc_score.items(), key=lambda x: -x[1])[:RERANK_DOCS]]

        if q_emb is not None and _np is not None and top_docs:
            picked = _doc_rerank(conn, q_emb, top_docs, rrf, docs_map)
            rerank_state = "ok"
        else:
            picked = [(d, None) for d in top_docs]

        results = []
        for cname, cid_best in picked:
            cids = [c for c, m in docs_map.items() if m[0] == cname]
            if cid_best is None or cid_best not in docs_map:
                cid_best = max(cids, key=lambda c: rrf.get(c, 0.0))
            cname, date, num, pt, heading, text = docs_map[cid_best]
            results.append(
                {
                    "chunk_id": cid_best,
                    "canonical_name": cname,
                    "date": date,
                    "number": num,
                    "point": pt,
                    "heading": heading,
                    "text": text,
                    "rrf_score": round(rrf.get(cid_best, 0.0), 6),
                }
            )
        results = results[:limit]
        meta = {
            "corpus": str(path),
            "query": query,
            "fts_hits": len(fts_rows),
            "vector_hits": len(vec_rows),
            "vector": vec_state,
            "prf": prf_state,
            "rerank": rerank_state,
            "accelerator": "numpy" if _np is not None else "python",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "scope": "active-only",
        }
        return results, meta
    finally:
        conn.close()
