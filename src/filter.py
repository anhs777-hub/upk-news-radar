"""키워드/중복/시간 필터 + 자체 점수 랭킹."""
from __future__ import annotations
import re
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser  # type: ignore

from . import store


def compute_window(now: datetime, state: dict) -> tuple[datetime, str]:
    """동적 cutoff 계산. (cutoff_dt, 인간친화 설명).

    - 최초 실행: 최근 72h
    - 이후: 마지막 실행 - 2h(안전 마진)
    - 최소 12h, 최대 14일 안전 장치
    """
    last = state.get("last_run_at")
    max_lookback = timedelta(days=14)
    min_lookback = timedelta(hours=12)

    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            cutoff = last_dt - timedelta(hours=2)
        except Exception:
            cutoff = now - timedelta(hours=72)
    else:
        cutoff = now - timedelta(hours=72)

    if (now - cutoff) > max_lookback:
        cutoff = now - max_lookback
        note = "마지막 실행 너무 오래 전 → 14일 제한 적용"
    elif (now - cutoff) < min_lookback:
        cutoff = now - min_lookback
        note = "마지막 실행 직후 → 최소 12h 윈도우 적용"
    else:
        hours = round((now - cutoff).total_seconds() / 3600, 1)
        note = f"마지막 실행 이후 {hours}h 윈도우"
    return cutoff, note


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = dateparser.parse(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _all_keywords(fronts: list[dict]) -> list[str]:
    kws: list[str] = []
    for f in fronts:
        kws.extend(f.get("keywords_ko", []))
        kws.extend(f.get("keywords_en", []))
    return [k.lower() for k in kws if k]


def _has_keyword(text: str, kws: list[str]) -> bool:
    t = text.lower()
    return any(k in t for k in kws)


def _normalize_title_for_dedup(title: str) -> str:
    """뉴스 제목 정규화: 출처/태그/구두점 제거 후 핵심만 남김."""
    if not title:
        return ""
    t = title
    # 출처 제거 (구글 뉴스 RSS는 마지막에 ' - 매체명' 붙음)
    t = re.sub(r'\s*[-–—|]\s*[^-–—|]{1,40}$', '', t)
    # 대괄호/소괄호 태그 제거 [속보] (단독) 등
    t = re.sub(r'[\[\(][^\]\)]{1,20}[\]\)]', '', t)
    # 한글/영문/숫자만 남김
    t = re.sub(r'[^\w가-힣]+', '', t)
    return t.lower()


def _title_trigrams(title: str) -> set:
    """정규화 후 문자 3-gram 집합 (자카드 유사도용)."""
    norm = _normalize_title_for_dedup(title)
    if len(norm) < 3:
        return {norm} if norm else set()
    return {norm[i:i+3] for i in range(len(norm) - 2)}


def _is_near_duplicate(grams: set, seen_grams: list, threshold: float = 0.30) -> bool:
    """이전에 본 제목들과 자카드 유사도 비교. threshold 이상이면 중복."""
    if not grams:
        return False
    for prev in seen_grams:
        if not prev:
            continue
        inter = len(grams & prev)
        if inter == 0:
            continue
        union = len(grams | prev)
        if union and inter / union >= threshold:
            return True
    return False


def filter_items(
    items: list[dict],
    fronts: list[dict],
    cfg_filter: dict,
    hours: int | None = None,
    state_path=None,
) -> list[dict]:
    """키워드 매칭 + 시간 + 중복 제거 + 최소 길이.

    hours가 명시되면 해당 시간 내 고정 윈도우.
    None이면 compute_window()로 동적 cutoff 계산 (증분 모드).
    state_path가 주어지면 채널별 _radarstate.json을 사용.
    """
    min_chars = int(cfg_filter.get("min_title_chars", 10))
    now = datetime.now(timezone.utc)
    if hours is not None:
        cutoff = now - timedelta(hours=int(hours))
    else:
        state = store.read_state(state_path)
        cutoff, _note = compute_window(now, state)
    kws = _all_keywords(fronts)

    seen_ids: set[str] = set()
    seen_grams: list[set] = []
    out: list[dict] = []

    for it in items:
        title = (it.get("title") or "").strip()
        if len(title) < min_chars:
            continue
        # 시간 필터 (published 없으면 통과)
        pub = _parse_dt(it.get("published"))
        if pub and pub < cutoff:
            continue
        # 키워드 필터 (제목+요약)
        text = title + " " + (it.get("summary") or "")
        if kws and not _has_keyword(text, kws):
            continue
        # 중복: id 우선 + 제목 trigram 자카드 유사도로 근접 중복 판정
        iid = it.get("id") or title
        if iid in seen_ids:
            continue
        grams = _title_trigrams(title)
        if _is_near_duplicate(grams, seen_grams):
            continue
        seen_ids.add(iid)
        if grams:
            seen_grams.append(grams)
        out.append(it)
    return out


# ---------------------------------------------------------------------------
# 자체 점수 랭킹 — LLM 호출 전 상위 N건으로 제한 (토큰 절약)
# ---------------------------------------------------------------------------
_NUM_UNIT_RE = re.compile(r"(조원|억원|만원|배|년|%|퍼센트|억|조)")


def _self_score(item: dict, fronts: list[dict]) -> float:
    title = (item.get("title") or "")
    summary = (item.get("summary") or "")
    text = (title + " " + summary).lower()

    score = 0.0
    # 분야 키워드 매칭 수 × 2
    kw_hits = 0
    for f in fronts:
        for k in (f.get("keywords_ko", []) + f.get("keywords_en", [])):
            if k and k.lower() in text:
                kw_hits += 1
    score += kw_hits * 2

    # 한국어 소스 +1
    if (item.get("lang") or "").lower() == "ko":
        score += 1

    # 제목에 숫자/금액 단위 포함 +1
    if _NUM_UNIT_RE.search(title):
        score += 1

    # 24시간 내 발행 +1
    pub = item.get("published")
    if pub:
        try:
            dt = dateparser.parse(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= datetime.now(timezone.utc) - timedelta(hours=24):
                score += 1
        except Exception:
            pass

    return score


def rank_and_cap(items: list, fronts: list, max_items: int = 100) -> list:
    """items에 자체 점수를 매긴 뒤 내림차순으로 상위 max_items만 반환."""
    if not items:
        return []
    scored = [(it, _self_score(it, fronts)) for it in items]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [it for it, _ in scored[:max_items]]
