"""언론사 떡상 감시기(newsradar) — 추적 풀 시계열 + 속도 등급.

핵심 책임:
- track.json: video_id별 조회수 시계열을 매일 append (추이 로깅)
- Δ24h(어제 대비 증가) 계산
- 등급 분류: 🔥hot / 📈rising / 🧊base
    · hot   = 업로드 hot_age_days 이내 + view ≥ hot_views (급가속)
    · rising= view ≥ track_threshold + Δ24h ≥ rising_delta (가속 지속)
    · base  = view ≥ track_threshold 이지만 느리게/둔화 (대형채널 일상)

판정은 절대값 단독이 아니라 "절대값 × 도달속도" 조합이다.
설계 근거: docs/언론사-떡상-감시기-기획.md §2, §4.1b
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# 이 기간 안의 영상은 '초반 폭발'이 delta에 안 잡힐 수 있어 추정속도도 함께 본다.
NEW_WINDOW_DAYS = 3.0


def _parse_iso(s: str) -> datetime | None:
    """ISO8601 → aware datetime. 실패 시 None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_days(published_at: str, now: datetime) -> float:
    """업로드 후 경과 일수. 알 수 없으면 큰 값(999)."""
    pub = _parse_iso(published_at)
    if pub is None:
        return 999.0
    return (now - pub).total_seconds() / 86400.0


def update_track(track: dict, items: list[dict], date: str, threshold: int,
                 now: datetime | None = None) -> dict:
    """추적 풀에 오늘치 조회수를 append.

    - track_threshold 이상 도달한 video_id는 추적 풀에 등록(최초 1회)
    - 이미 추적 중인 영상은 임계 미만이어도 계속 시계열 누적(냉각 추적용)
    - 같은 날짜 중복 실행은 마지막 측정값으로 갱신(멱등)

    각 측정점에 실제 측정 시각(at)을 남긴다. 감지 간격이 하루가 아닐 때
    (예: 7시간 뒤 재감지) 증가량을 24h로 환산하려면 시각이 필요하다.
    date만 있으면 7시간 증가분이 '하루 증가량'으로 둔갑해 속도가 축소된다.

    track 구조:
      { video_id: {
          title, channel_title, whitelist_name, published_at, url,
          duration_sec, format,
          series: [{date, at, views}, ...]
      } }
    """
    at = (now or datetime.now(timezone.utc)).isoformat()
    by_id = {it.get("video_id"): it for it in items if it.get("video_id")}

    for vid, it in by_id.items():
        views = int(it.get("view_count", 0) or 0)
        already = vid in track
        if not already and views < threshold:
            continue  # 아직 임계 미달 + 미등록 → 추적 안 함

        entry = track.get(vid) or {
            "title": it.get("title", ""),
            "channel_title": it.get("channel_title", ""),
            "whitelist_name": it.get("whitelist_name", ""),
            "published_at": it.get("published_at", ""),
            "url": it.get("url", ""),
            "thumbnail": it.get("thumbnail", ""),
            "duration_sec": it.get("duration_sec", 0),
            "format": it.get("format", "unknown"),
            "is_live": it.get("is_live", False),
            "channel_subscriber_count": 0,
            "series": [],
        }
        # 메타 최신화(빈 값만 보충)
        for k in ("title", "channel_title", "whitelist_name", "published_at", "url", "thumbnail"):
            if not entry.get(k) and it.get(k):
                entry[k] = it[k]
        # 구독자수는 시간에 따라 변하는 값이라 새 값이 오면 항상 갱신한다.
        # (0은 조회 실패/비공개일 수 있으므로 덮지 않고 직전 값을 유지)
        subs = int(it.get("channel_subscriber_count", 0) or 0)
        if subs > 0:
            entry["channel_subscriber_count"] = subs
        # 라이브 보정: actualStartTime 기반 published_at은 '항상' 갱신(빈 값 아니어도 덮어씀)
        if it.get("is_live") and it.get("published_at"):
            entry["published_at"] = it["published_at"]
            entry["is_live"] = True
        if not entry.get("duration_sec") and it.get("duration_sec"):
            entry["duration_sec"] = it["duration_sec"]
            entry["format"] = it.get("format", entry.get("format", "unknown"))

        series = entry.get("series", [])
        # 같은 날짜면 덮어쓰기(멱등), 아니면 추가
        series = [p for p in series if p.get("date") != date]
        series.append({"date": date, "at": at, "views": views})
        series.sort(key=lambda p: p.get("date", ""))
        entry["series"] = series
        track[vid] = entry

    return track


def delta_24h(entry: dict) -> int | None:
    """마지막 두 측정점의 증가량을 24시간으로 환산. 점이 1개뿐이면 None(속도 미정).

    감지 간격은 일정하지 않다(7시간 뒤일 수도, 3일 뒤일 수도). 단순 차이를 쓰면
    짧은 간격일수록 속도가 축소돼 급상승이 base로 묻힌다. 실제 경과시간으로
    나눠 하루치로 환산한다.

    측정점에 시각(at)이 없는 구 데이터는 date 차이로 대체하고, 그마저 없으면
    간격을 1일로 본다(구 동작과 동일).
    """
    series = entry.get("series", [])
    if len(series) < 2:
        return None
    prev, last = series[-2], series[-1]
    diff = int(last["views"]) - int(prev["views"])

    gap = _series_gap_days(prev, last)
    if gap is None or gap <= 0:
        return diff  # 간격 불명 → 구 동작(그대로 하루치 취급)
    # 너무 촘촘한 재감지(예: 10분)는 환산 배수가 폭주하므로 하한 2시간
    gap = max(gap, 2 / 24)
    return int(round(diff / gap))


def _series_gap_days(prev: dict, last: dict) -> float | None:
    """두 측정점 사이 경과 일수. at(시각) 우선, 없으면 date로 폴백."""
    t0, t1 = _parse_iso(prev.get("at", "")), _parse_iso(last.get("at", ""))
    if t0 and t1:
        return (t1 - t0).total_seconds() / 86400.0
    d0, d1 = _parse_iso(prev.get("date", "")), _parse_iso(last.get("date", ""))
    if d0 and d1:
        return (d1 - d0).total_seconds() / 86400.0
    return None


def latest_views(entry: dict) -> int:
    series = entry.get("series", [])
    return int(series[-1]["views"]) if series else 0


def classify(entry: dict, cfg: dict, now: datetime) -> dict:
    """단일 추적 항목 → 등급 분류 결과.

    반환: {tier, views, delta, age, long, reason}
      tier ∈ {"hot", "rising", "base", "watch"}
        watch = 추적 풀엔 있으나 아직 임계 미만(승급 대기)
    """
    track_th = int(cfg.get("track_threshold", 1_000_000))
    min_long = int(cfg.get("min_long_sec", 180))
    score_base = int(cfg.get("score_base", 100_000)) or 100_000  # 하루 이만큼 증가 = 지수 100

    views = latest_views(entry)
    delta = delta_24h(entry)  # None 가능(첫날 등 점 1개)
    a = age_days(entry.get("published_at", ""), now)
    is_long = int(entry.get("duration_sec", 0) or 0) > min_long

    # 속도 근사(첫날용): 조회수 / 나이(일). 0나눗셈 방지 하한 0.5일.
    velocity_est = views / max(a, 0.5)

    # 등급 판정 — 절대 기준(하루 증가량 = raw speed)으로 통일.
    #  hot   : raw ≥ score_base            (하루 10만+ 증가 = 지수 100+) — 진짜 떡상
    #  rising: raw ≥ score_base × 0.3      (하루 3만+ = 지수 30+)        — 오르는 중
    #  base  : 그 미만                      (식었거나 미미)
    #  watch : 추적 임계 미만
    # 첫날(delta 없음)은 velocity_est(추정)로 같은 절대선 적용 → 날마다 안 흔들림.
    #
    # 신규 영상(NEW_WINDOW_DAYS 이내)은 delta와 velocity_est 중 '큰 값'을 쓴다.
    # 업로드 직후 폭발한 영상(예: 8시간 만에 14만)은 그 폭발이 첫 측정 이전에
    # 끝나 delta에 안 잡힌다. delta만 보면 두 번째 측정에서 곧장 base로 강등돼
    # 양산형 조기 포착을 놓친다. 오래된 영상은 총조회수÷나이가 과거 실적까지
    # 평균내 거품이 되므로 기존대로 delta만 쓴다.
    if delta is None:
        raw, is_est = float(velocity_est), True
    elif a <= NEW_WINDOW_DAYS and velocity_est > delta:
        raw, is_est = float(velocity_est), True
    else:
        raw, is_est = float(delta), False
    suffix = " (추정)" if is_est else ""
    if views < track_th:
        tier, reason = "watch", "임계 미만(승급 대기)"
    elif raw >= score_base:
        tier, reason = "hot", f"하루 +{int(raw):,}{suffix}"
    elif raw >= score_base * 0.3:
        tier, reason = "rising", f"하루 +{int(raw):,}{suffix}"
    else:
        tier, reason = "base", f"하루 +{int(raw):,}{suffix}"

    return {
        "tier": tier,
        "views": views,
        "delta": delta,
        "age": round(a, 1),
        "velocity_est": int(velocity_est),
        "raw_speed": int(raw),   # 등급·속도지수가 함께 쓰는 판정 원값(하루 증가량)
        "is_est": is_est,
        "long": is_long,
        "reason": reason,
    }


# 등급 정렬 우선순위 (낮을수록 위)
_TIER_ORDER = {"hot": 0, "rising": 1, "base": 2, "watch": 3}


def build_leaderboard(track: dict, cfg: dict, now: datetime, include_short: bool = False) -> list[dict]:
    """추적 풀 전체 → 리더보드 정렬 리스트.

    - 숏폼 제외(기본). 떡상 재현 가치가 달라 메인 리스트에선 뺀다.
    - 첫날(속도 미측정, tier="new")은 velocity_est(조회수÷나이)로 상대 순위.
      상위 일부만 🔥hot, 나머지는 🧊base로 분리 → "다 빨강" 방지.
    - 둘째날부터 delta(Δ24h)가 잡히면 classify가 rising/base로 직접 판정.
    """
    rows: list[dict] = []
    for vid, entry in track.items():
        c = classify(entry, cfg, now)
        if not include_short and not c["long"]:
            continue  # 숏폼 제외
        subs = int(entry.get("channel_subscriber_count", 0) or 0)
        # 효율 = 조회수 / 구독자. 속도지수(절대량)가 못 보는 '체급 대비'를 채운다.
        # 구독자 500만이 하루 30만 오르는 것과 3만 채널이 15만 오르는 것은
        # 속도지수로는 전자가 위지만, 레퍼런스 가치는 후자가 크다.
        # 구독자 미상(0)은 계산 불가 → None (0배와 구분해야 정렬에서 안 섞인다)
        eff = (c["views"] / subs) if subs > 0 else None
        rows.append({
            "video_id": vid,
            "title": entry.get("title", ""),
            "channel_title": entry.get("channel_title", ""),
            "whitelist_name": entry.get("whitelist_name", ""),
            "published_at": entry.get("published_at", ""),
            "url": entry.get("url", ""),
            "thumbnail": entry.get("thumbnail", ""),
            "format": entry.get("format", "unknown"),
            "series": entry.get("series", []),
            "subscriber_count": subs,
            "efficiency": round(eff, 2) if eff is not None else None,
            **c,
        })

    # 등급은 classify가 절대 기준으로 이미 정함(hot/rising/base/watch). 상대 분리 불필요.

    # 속도지수 = 절대 지수. "하루 score_base(기본 10만) 증가 = 지수 100" 고정 기준.
    #  - delta(실측 Δ24h) 있으면 그걸, 없으면(첫날) velocity_est(조회수÷나이, 추정)를 원값으로.
    #  - Δ24h ÷ 기준선 × 100. 기준선 고정이라 날마다 안 흔들리고 절대 비교 가능(나이 무관).
    #  - 전 분류 공통 기준: 조회수가 곧 수익이라 분류를 봐줄 이유 없음(낮으면 돈 안 됨).
    score_base = int(cfg.get("score_base", 100_000)) or 100_000
    for r in rows:
        # 원값은 classify가 정한 raw_speed를 그대로 쓴다. 여기서 재계산하면
        # 등급(hot)과 점수(25)가 서로 다른 기준으로 갈려 화면에서 어긋난다.
        raw = float(r.get("raw_speed", r["delta"] if r["delta"] is not None else r["velocity_est"]))
        r["velocity_score"] = round(raw / score_base * 100)
        r["reason"] = f"속도지수 {r['velocity_score']}" + (" (추정)" if r.get("is_est") else "")

    def sort_key(r: dict):
        return (
            _TIER_ORDER.get(r["tier"], 9),
            -r["velocity_score"],
            -r["views"],
        )

    rows.sort(key=sort_key)
    return rows
