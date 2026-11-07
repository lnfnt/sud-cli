# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

import sys, os, sqlite3, json, math, struct, urllib.request
from pathlib import Path

DB_PATH = Path(os.environ.get('SUD_VSRF_DB', '/home/user/workspace/datasets/vsrf-corpus/db/vsrf-corpus.db'))
EMBED_URL = 'http://198.51.100.1:11434/api/embeddings'
MODEL = 'bge-m3:latest'

def search_practice(query, limit=5):
    if not DB_PATH.exists():
        print(f"Ошибка: База судебной практики {DB_PATH} не найдена.")
        return
        
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    
    import re
    words = [w for w in re.findall(r'[а-яёa-z0-9]+', query.lower()) if len(w) > 2]
    fts_query = ' OR '.join(words) if words else query
    
    # 1. BM25 Search
    fts_results = cur.execute('''
        SELECT c.chunk_id, d.canonical_name, d.court, d.date, d.number, c.point, c.heading, c.text, rank
        FROM chunks_fts f
        JOIN chunks c ON f.rowid = c.rowid
        JOIN documents d ON c.doc_sha = d.sha256
        WHERE chunks_fts MATCH ? AND c.status = 'active'
        ORDER BY rank
        LIMIT 25
    ''', (fts_query,)).fetchall()
    
    print(f"\n================================================================================")
    print(f" ПОИСК ПО СУДЕБНОЙ ПРАКТИКЕ ВС И ВАС РФ: \"{query}\"")
    print(f" Статус: ТОЛЬКО ДЕЙСТВУЮЩИЕ АКТЫ (Law Zero фильтр: status='active')")
    print(f"================================================================================\n")
    
    if not fts_results:
        print("Ничего не найдено.")
        conn.close()
        return
        
    for idx, r in enumerate(fts_results[:limit], 1):
        cid, cname, court, dt, num, pt, heading, text, rank = r
        act_title = f"Постановление Пленума {court} от {dt} № {num}" if num != 'NN' else cname
        
        # Check if text itself says overturned
        is_overturned = 'признан утратившим силу' in text.lower() or 'утратил силу' in text.lower()
        
        print(f"[{idx}] {act_title} | {heading}")
        if is_overturned:
            print("    ⚠️  ВНИМАНИЕ: ДАННОЕ ПОЛОЖЕНИЕ УТРАТИЛО СИЛУ / ЗАМЕНЕНО БОЛЕЕ ПОЗДНИМ ПЛЕНУМОМ!")
        print(f"    Файл: {cname}")
        snippet = text.replace('\n', ' ').strip()[:350]
        print(f"    Цитата: \"{snippet}...\"\n")
        
    conn.close()

if __name__ == '__main__':
    q = ' '.join(sys.argv[1:]) if len(sys.argv) > 1 else 'снижение неустойки 333 ГК РФ'
    search_practice(q)
