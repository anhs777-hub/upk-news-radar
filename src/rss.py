"""RSS 수집. feedparser로 sources를 돌고 표준 dict로 변환."""
from __future__ import annotations
import hashlib
import sys
from datetime import datetime, timezone
from typing import Any

try:
    import feedparser  # type: ignore
except ImportError:  # 가동 X — import 실패해도 모듈 자체는 로드되어야 함
    feedparser = None  # type: ignore


def _hash_id(url: str, title: str) -> str:
    h = hashlib.sha1()
    h.update((url + "::" + title).encode("utf-8", errors="ignore"))
    return h.hexdigest()[:12]


def _entry_published(entry: Any) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        v = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if v:
            try:
                dt = datetime(*v[:6], tzinfo=timezone.utc)
                return dt.isoformat()
            except Exception:
                continue
    return None


def fetch_source(source: dict) -> list[dict]:
    """단일 RSS 소스에서 항목 리스트 반환."""
    if feedparser is None:
        print(f"[rss] feedparser 미설치 — {source.get('name')} 건너뜀", file=sys.stderr)
        return []
    url = source["url"]
    name = source.get("name", url)
    lang = source.get("lang", "en")
    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        print(f"[rss] {name} 실패: {e}", file=sys.stderr)
        return []

    out: list[dict] = []
    for entry in getattr(parsed, "entries", []) or []:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue
        summary = (getattr(entry, "summary", "") or "").strip()
        published = _entry_published(entry)
        out.append({
            "id": _hash_id(link, title),
            "source": name,
            "lang": lang,
            "title": title,
            "link": link,
            "summary": summary[:1000],
            "published": published,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
    return out


def fetch_all(sources: list[dict]) -> list[dict]:
    items: list[dict] = []
    for src in sources:
        got = fetch_source(src)
        print(f"[rss] {src.get('name')}: {len(got)}건")
        items.extend(got)
    return items


# 별칭 (스킬/외부 호출 호환)
def collect_all(sources: list[dict]) -> list[dict]:
    return fetch_all(sources)


def fetch_rss(url: str, source_name: str = "", lang: str = "ko") -> list[dict]:
    """단일 URL → 항목 리스트. fetch_source 래퍼."""
    return fetch_source({"url": url, "name": source_name or url, "lang": lang})


def google_news_search_url(query: str, days: int, hl: str = "ko") -> str:
    """구글 뉴스 RSS 시간 검색 URL 생성."""
    from urllib.parse import quote
    when = f"when:{days}d" if days >= 1 else "when:1d"
    q = f"{query} {when}"
    return f"https://news.google.com/rss/search?q={quote(q)}&hl={hl}"


def collect_recovery(fronts: list[dict], days: int) -> list[dict]:
    """간격이 24h 넘으면 호출. 분야 키워드 × when:Nd 검색.

    분야당 키워드 2개만 사용 (호출수/토큰 절제).
    """
    items: list[dict] = []
    for f in fronts:
        kws = (f.get("keywords_ko", []) + f.get("keywords_en", []))[:2]
        for kw in kws:
            url = google_news_search_url(kw, days)
            try:
                fetched = fetch_rss(url, source_name=f"보강검색 - {kw}", lang="ko")
                items.extend(fetched)
            except Exception as e:
                print(f"[rss] 보강검색 {kw} 실패: {e}", file=sys.stderr)
    return items
