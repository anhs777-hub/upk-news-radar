"""YouTube 데이터 → LLM 빈 자리 분석.

입력: 분야별로 묶인 비디오 리스트
출력: 분야당 {trend_summary, common_angles, missing_angles, suggested_video, ...}
"""
from __future__ import annotations
import json
import sys
from typing import Any

from . import llm  # _call_claude / _safe_json 재사용


YT_ANALYST_SYSTEM = """너는 한국 유튜브 과학 채널 PD다. 같은 주제의 영상 N개를 분석해 (1) 공통 각도, (2) 빠진 각도, (3) 빈 자리를 노린 영상 후크를 제안한다.

[톤 절대 규칙]
- Hook 첫 30초 안에 2인칭(내/네/당신) 또는 일상 사물 1개 이상 필수
- 학술 한자어 금지, 본능 단어 우선
- 시리즈 의존 금지

[롱폼 vs 숏폼 처리 원칙]
- 빈 자리 분석(missing_angles)과 제안 영상(suggested_video)은 반드시 **롱폼 영상 기준으로만** 작성한다.
- 숏폼은 "이 주제가 숏폼에서도 화제다"라는 트렌드 시그널로만 활용한다.
- 롱폼이 0개면 빈 자리 분석을 강제하지 말고 "데이터 부족"으로 표시하라.

[estimated_demand 점수 기준 — 일관성 유지 필수]
5 = 메이저 사건/뉴스 직접 발생 (실적 발표, 신제품 출시, 사고, 정책 변화) + 시청자 일상 직접 영향
4 = 명확한 트렌드 형성, 주요 채널 다수가 영상 제작 중, 검색량 상승 추정
3 = 꾸준한 관심, 안정적 검색량
2 = 일부 마니아층만 관심, 산발적
1 = 대중 무관심, 학계 내부

[competition_level 점수 기준 — 일관성 유지 필수]
5 = 영상 30+개, 톱티어 채널 다수 진입 완료, 조회수 100만+ 영상 다수
4 = 영상 15~30개, 주요 채널 절반 이상 진입, 조회수 30~100만 다수
3 = 영상 5~15개, 일부 채널만, 조회수 10~30만
2 = 영상 1~5개, 마이너 채널만, 조회수 ~10만
1 = 영상 거의 없음, 무주공산

위 두 기준을 매번 적용해 동일 데이터엔 동일 점수가 나오도록 하라.

[missing_angles 작성 원칙 — 매우 중요]
빠진 각도는 추상적 학술 영역(예: "물리학적 원리")이 아니라 **시청자 시점의 구체적 질문**으로 적어라.
- 나쁨: "양자터널링의 물리학", "HBM 구조 원리"
- 좋음: "내 폰 칩이 정확히 어떻게 0과 1을 구분하나", "5나노가 사람 머리카락보다 얼마나 작은지 비교", "내 노트북 가격이 왜 떨어지지 않나"
2인칭(내/네/당신) 또는 일상 사물 비교를 최소 1개 이상 포함하라.

[CRITICAL OUTPUT CONTRACT]
Respond with ONLY a single raw JSON object. NO markdown. NO code fences. NO commentary.
"""


def analyze_front(front: dict, videos: list[dict], top_n: int = 15) -> dict:
    """한 분야의 비디오들을 분석해 빈 자리 카피 생성."""
    if not videos:
        return {}
    # format 필드 기준 (long/short/unknown) — 3분(180초) 경계
    # 옛 데이터(ambiguous 포함)는 duration_sec 기반으로 재분류
    def _fmt(v):
        sec = v.get("duration_sec", 0) or 0
        if sec == 0:
            # duration 정보 없으면 기존 format 필드 사용 (ambiguous는 short로 흡수)
            f = v.get("format")
            if f == "long":
                return "long"
            if f == "short" or f == "ambiguous":
                return "short"
            return "unknown"
        if sec <= 180:
            return "short"
        return "long"

    longs = [v for v in videos if _fmt(v) == "long"]
    shorts = [v for v in videos if _fmt(v) == "short"]
    unknowns = [v for v in videos if _fmt(v) == "unknown"]
    longs_sorted = sorted(longs, key=lambda x: x.get("view_count", 0), reverse=True)
    shorts_sorted = sorted(shorts, key=lambda x: x.get("view_count", 0), reverse=True)
    unknowns_sorted = sorted(unknowns, key=lambda x: x.get("view_count", 0), reverse=True)

    # 분석 입력은 long + unknown 합쳐서 조회수순
    long_plus_unknown = sorted(longs + unknowns, key=lambda x: x.get("view_count", 0), reverse=True)
    compact_long = [
        {
            "title": v.get("title", "")[:120],
            "channel": v.get("channel_title", ""),
            "views": v.get("view_count", 0),
            "likes": v.get("like_count", 0),
            "published": (v.get("published_at", "") or "")[:10],
            "duration_sec": v.get("duration_sec", 0),
        }
        for v in long_plus_unknown[:top_n]
    ]
    long_count = len(longs)
    short_count = len(shorts)
    ambiguous_count = 0  # 분류 체계 2분류화 이후 항상 0 (스키마 호환용)
    unknown_count = len(unknowns)

    user_msg = (
        "[CRITICAL OUTPUT CONTRACT]\n"
        "Respond with ONLY a single raw JSON object. NO markdown. NO commentary.\n"
        "Schema:\n"
        '{"trend_summary": "...", '
        '"hot_topics": ["...", "...", "..."], '
        '"common_angles": ["...", "...", "..."], '
        '"missing_angles": ["...", "..."], '
        '"suggested_video": {"hook": "내 ~", "title": "...", "why_gap": "..."}, '
        '"estimated_demand": 1-5, '
        '"competition_level": 1-5}\n\n'
        f"[FRONT] {front.get('name','')} (id={front.get('id','')})\n"
        f"[LONG-FORM VIDEOS] 분석 대상 — 조회수 상위 {len(compact_long)}개 (총 롱폼 {long_count}개):\n"
        + json.dumps(compact_long, ensure_ascii=False, indent=2)
        + f"\n\n[SHORT-FORM CONTEXT] 같은 분야 숏폼은 {short_count}개 존재 (트렌드 신호용, 분석 대상 X)\n"
        "\n빈 자리 분석은 위 롱폼 기준으로만 작성하라. 롱폼이 적으면 trend_summary에 명시하라.\n"
        "Now output the JSON object only:"
    )
    try:
        text = llm._call_claude(user_msg, system=YT_ANALYST_SYSTEM, timeout=180)
        data = llm._safe_json(text)
        if isinstance(data, dict):
            data["video_count"] = len(videos)
            data["long_count"] = long_count
            data["short_count"] = short_count
            data["ambiguous_count"] = ambiguous_count
            data["unknown_count"] = unknown_count
            top_pool = long_plus_unknown
            data["top_views"] = (
                top_pool[0].get("view_count", 0) if top_pool
                else (shorts_sorted[0].get("view_count", 0) if shorts_sorted else 0)
            )
            # 0 views 항목은 제외 (enrich 실패 케이스)
            top_pool_valid = [v for v in top_pool if (v.get("view_count", 0) or 0) > 0]
            def _vid_brief(v):
                return {
                    "title": v.get("title", ""),
                    "channel_title": v.get("channel_title", ""),
                    "view_count": v.get("view_count", 0),
                    "url": v.get("url", ""),
                    "duration_sec": v.get("duration_sec", 0),
                    "published_at": (v.get("published_at", "") or "")[:10],
                    "channel_subscriber_count": v.get("channel_subscriber_count", 0),
                    "view_to_sub_ratio": v.get("view_to_sub_ratio", 0),
                    "copy_score": v.get("copy_score", 0),
                }
            data["top_long_videos"] = [_vid_brief(v) for v in top_pool_valid[:5]]
            shorts_valid = [v for v in shorts_sorted if (v.get("view_count", 0) or 0) > 0]
            data["top_short_videos"] = [_vid_brief(v) for v in shorts_valid[:5]]
            # 카피 가치 Top — 분야별 신생/중형 폭발 영상
            copy_pool = sorted(
                [v for v in (top_pool_valid + shorts_valid) if (v.get("copy_score") or 0) >= 4],
                key=lambda v: (-(v.get("copy_score") or 0), -(v.get("view_to_sub_ratio") or 0)),
            )
            data["top_copy_value_videos"] = [_vid_brief(v) for v in copy_pool[:5]]
            data["opportunity_tier"] = _compute_opportunity(
                data.get("estimated_demand"), data.get("competition_level")
            )
            data["top_video_url"] = (
                top_pool[0].get("url", "") if top_pool
                else (shorts_sorted[0].get("url", "") if shorts_sorted else "")
            )
            return data
    except Exception as e:
        print(f"[ytanalyze] {front.get('id')} 실패: {e}", file=sys.stderr)
    return {}


def _compute_opportunity(demand, competition) -> str:
    """수요/경쟁 → 기회 등급.
    score = demand*1.5 - competition (수요 가중, 경쟁 페널티)
    임계 컷오프 대신 연속 점수로 변환해 1점 변동의 등급 점프 완화.
    """
    try:
        d = int(demand or 0)
        c = int(competition or 0)
    except Exception:
        return ""
    if d == 0 and c == 0:
        return ""
    score = d * 1.5 - c
    # score 범위 대략 -5 ~ +6.5
    if score >= 5:
        return "gold"   # 황금자리 (수요5경쟁2 / 수요4경쟁1 등)
    if score >= 3:
        return "ok"     # 좋은 자리 (수요5경쟁4 / 수요4경쟁3)
    if score >= 1:
        return "mixed"  # 평범 (수요3경쟁3)
    if score >= -1:
        return "weak"   # 약함 (수요2경쟁3)
    if d <= 2 and c >= 4:
        return "trap"   # 함정 (수요낮은데 경쟁만 많음)
    return "gray"      # 무관심


def analyze_all_fronts(items_by_front: dict[str, list[dict]], fronts: list[dict]) -> list[dict]:
    """전체 분석. 분야당 1번씩 LLM 호출."""
    results: list[dict] = []
    for f in fronts:
        videos = items_by_front.get(f.get("id", ""), [])
        if not videos:
            continue
        print(f"[ytanalyze] {f.get('id')} {len(videos)}개 분석 중...", file=sys.stderr)
        analysis = analyze_front(f, videos)
        if analysis:
            results.append({
                "front_id": f.get("id", ""),
                "front_name": f.get("name", ""),
                "front_color": f.get("color", "#888"),
                "analysis": analysis,
            })
    return results
