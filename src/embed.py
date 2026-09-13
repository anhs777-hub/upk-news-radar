"""로컬 임베딩 + 코사인 유사도 매칭.

BGE-M3 (BAAI/bge-m3) 사용. 첫 호출 시 모델 다운로드 (~2GB).
sentence-transformers 의존성 필요.
"""
from __future__ import annotations
from typing import Iterable

_MODEL = None
_MODEL_NAME = "BAAI/bge-m3"


def _get_model():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        print(f"[embed] 모델 로드: {_MODEL_NAME} (최초 실행 시 다운로드)")
        _MODEL = SentenceTransformer(_MODEL_NAME)
    return _MODEL


def embed(texts: list[str]):
    """텍스트 리스트 → numpy 배열 (n, dim). 내부적으로 정규화."""
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


def match_top(query_text: str, candidates: list[dict], text_key: str, top_k: int = 5, min_score: float = 0.35) -> list[dict]:
    """쿼리 1개 → 후보 리스트 중 상위 top_k. 정규화 임베딩 가정 → 코사인 = 내적.

    각 결과는 후보 dict + ``score`` 필드 추가.
    """
    if not query_text or not candidates:
        return []
    import numpy as np
    texts = [query_text] + [(c.get(text_key) or "") for c in candidates]
    vecs = embed(texts)
    q = vecs[0]
    cands = vecs[1:]
    sims = (cands @ q)
    order = np.argsort(-sims)
    out: list[dict] = []
    for i in order[:top_k]:
        s = float(sims[i])
        if s < min_score:
            break
        item = dict(candidates[int(i)])
        item["score"] = round(s, 3)
        out.append(item)
    return out


def load_rss_items_window(daily_dir, end_date: str, days: int = 3) -> list[dict]:
    """end_date 포함 최근 N일치 RSS daily 파일을 합쳐서 반환 (id 중복 제거).

    daily_dir: pathlib.Path, end_date: 'YYYY-MM-DD'
    """
    from datetime import datetime, timedelta
    end = datetime.strptime(end_date, "%Y-%m-%d")
    seen: set = set()
    out: list[dict] = []
    for i in range(days):
        d = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        f = daily_dir / f"{d}.json"
        if not f.exists():
            continue
        try:
            import json as _json
            data = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in data.get("items", []) or []:
            iid = it.get("id")
            if iid and iid not in seen:
                seen.add(iid)
                out.append(it)
    return out


def attach_related_rss(analyses: list[dict], rss_items: list[dict], top_k: int = 5) -> int:
    """yt analyses 각각에 ``related_rss`` 추가. 매칭된 총 건수 반환.

    RSS 후보 임베딩은 한 번만 계산하고 모든 분야 쿼리에서 재사용.
    """
    if not analyses or not rss_items:
        return 0
    import numpy as np
    # 후보 RSS 평탄화
    cands = []
    for it in rss_items:
        if not it.get("id"):
            continue
        text = " ".join(filter(None, [it.get("title", ""), it.get("one_line_summary", ""), it.get("summary", "")]))
        cands.append({
            "id": it["id"],
            "title": it.get("title", ""),
            "one_line_summary": it.get("one_line_summary", ""),
            "front_id": it.get("front_id", ""),
            "_text": text,
        })
    if not cands:
        return 0
    # RSS 후보 임베딩 1회 계산
    cand_texts = [c["_text"] for c in cands]
    print(f"[embed] RSS {len(cand_texts)}건 임베딩 계산 중...")
    cand_vecs = embed(cand_texts)
    # 쿼리 일괄 수집
    queries = []
    for a in analyses:
        an = a.get("analysis", {}) or {}
        sv = an.get("suggested_video", {}) or {}
        parts = [
            a.get("front_name", ""),
            an.get("trend_summary", ""),
            " ".join(an.get("hot_topics", []) or []),
            sv.get("hook", ""),
            sv.get("title", ""),
        ]
        queries.append(" ".join(p for p in parts if p))
    # 쿼리 임베딩 1회 계산
    print(f"[embed] 쿼리 {len(queries)}건 임베딩 계산 중...")
    query_vecs = embed(queries)
    # 각 쿼리별 상위 매칭
    total = 0
    for i, a in enumerate(analyses):
        sims = cand_vecs @ query_vecs[i]
        order = np.argsort(-sims)
        matches = []
        for j in order[:top_k]:
            s = float(sims[j])
            if s < 0.35:
                break
            matches.append(cands[int(j)])
            matches[-1] = {**cands[int(j)], "score": round(s, 3)}
        a["related_rss"] = [
            {"id": m["id"], "title": m["title"], "one_line_summary": m["one_line_summary"], "front_id": m["front_id"], "score": m["score"]}
            for m in matches
        ]
        total += len(a["related_rss"])
    return total
