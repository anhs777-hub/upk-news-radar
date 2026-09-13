"""YouTube Data API v3 래퍼 (urllib only).

수집:
- collect_trending(region, category, max_results) — chart=mostPopular, 1유닛
- search_videos(query, days, max_results) — search.list, 100유닛
- enrich_stats(items) — videos.list로 통계 보강, 1유닛/50건
- collect_for_fronts(fronts, days, per_keyword, max_keywords_per_front)
"""
from __future__ import annotations
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any

API_BASE = "https://www.googleapis.com/youtube/v3"

_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration_seconds(iso: str) -> int:
    """ISO 8601 duration (PT5M30S) → 초. 실패 시 0."""
    if not iso:
        return 0
    m = _DURATION_RE.fullmatch(iso.strip())
    if not m:
        return 0
    h, mn, s = m.groups()
    return int(h or 0) * 3600 + int(mn or 0) * 60 + int(s or 0)


def classify_format(item: dict) -> str:
    """영상 포맷 분류: 'short' | 'long' | 'unknown'

    - duration 0초 → unknown
    - ≤180초(3분 이하) → short
    - >180초(3분 초과) → long
    """
    sec = parse_duration_seconds(item.get("duration", ""))
    if sec == 0:
        return "unknown"
    if sec <= 180:
        return "short"
    return "long"


def is_short_video(item: dict) -> bool:
    """하위호환용. classify_format='short'면 True."""
    return classify_format(item) == "short"

# 카테고리 ID (KR 기준)
CATEGORY_SCIENCE = "28"   # Science & Technology
CATEGORY_EDUCATION = "27"  # Education


# 이번 프로세스에서 429(쿼터 소진)로 판정된 키 인덱스. 다음 호출부터 건너뛴다.
_EXHAUSTED_KEYS: set[int] = set()


def _record_key_exhausted(key: str) -> None:
    """소진된 키를 data/_newsradar/_keystate.json에 기록 (대시보드 사용량 표시용)."""
    try:
        from datetime import datetime as _dt
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "_newsradar", "_keystate.json")
        today = _dt.utcnow().strftime("%Y-%m-%d")
        state = {"date": today, "usage": {}}
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("date") == today:
                    state = old
            except Exception:
                pass
        tail = key[-8:]
        entry = state["usage"].get(tail, {})
        entry["exhausted"] = True
        state["usage"][tail] = entry
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass  # 기록 실패는 무시 (본 기능에 영향 없음)


def _api_keys() -> list[str]:
    """다중 YouTube API 키 로드. 서로 다른 구글 계정/프로젝트의 키를 넣으면 쿼터가 합산된다.

    읽는 환경변수(있는 것만, 순서대로):
      YOUTUBE_API_KEY, YOUTUBE_API_KEY_2, YOUTUBE_API_KEY_3, ...
    또는 YOUTUBE_API_KEYS="key1,key2,key3" (쉼표 구분) 한 방에도 가능.
    """
    keys: list[str] = []
    multi = os.environ.get("YOUTUBE_API_KEYS", "").strip()
    if multi:
        keys.extend(k.strip() for k in multi.split(",") if k.strip())
    k1 = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if k1:
        keys.append(k1)
    i = 2
    while True:
        kn = os.environ.get(f"YOUTUBE_API_KEY_{i}", "").strip()
        if not kn:
            break
        keys.append(kn)
        i += 1
    # 중복 제거(순서 유지)
    seen: set[str] = set()
    uniq = [k for k in keys if not (k in seen or seen.add(k))]
    if not uniq:
        raise RuntimeError("YouTube API 키 없음. .env의 YOUTUBE_API_KEY(_2,_3…) 또는 대시보드 ⚙️에서 입력하세요.")
    return uniq


def _is_quota_error(body: str) -> bool:
    b = body.lower()
    return "quota" in b or "quotaexceeded" in b or "dailylimitexceeded" in b


def _get(path: str, params: dict) -> dict:
    """API 호출. 429/쿼터 초과면 다음 키로 자동 폴백(다른 계정 쿼터 순차 소진)."""
    keys = _api_keys()
    base_params = dict(params)
    last_err = None
    for idx, key in enumerate(keys):
        if idx in _EXHAUSTED_KEYS:
            continue  # 이번 프로세스에서 이미 소진된 키는 건너뜀
        p = dict(base_params)
        p["key"] = key
        url = f"{API_BASE}/{path}?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={"User-Agent": "news-radar/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            is_quota = (e.code == 429 or e.code == 403) and _is_quota_error(body)
            is_bad_key = e.code == 400 and "api key not valid" in body.lower()
            if is_quota or is_bad_key:
                # 쿼터 소진(429/403) 또는 무효 키(400) → 다음 키로 폴백
                if is_quota:
                    _EXHAUSTED_KEYS.add(idx)  # 쿼터 소진은 이 프로세스 내내 스킵
                    _record_key_exhausted(key)  # 대시보드 표시용 기록
                reason = "쿼터 소진" if is_quota else "무효 키"
                print(f"[yt] 키 #{idx+1} {reason} → 다음 키로 폴백 "
                      f"({idx+1}/{len(keys)})", file=sys.stderr)
                last_err = RuntimeError(f"YouTube API HTTP {e.code}: {reason}")
                continue
            raise RuntimeError(f"YouTube API HTTP {e.code}: {body[:300]}")
    # 모든 키 소진/무효
    raise last_err or RuntimeError("YouTube API: 사용 가능한 키 없음(전부 쿼터 소진/무효)")


def collect_trending(region: str = "KR", category: str = CATEGORY_SCIENCE, max_results: int = 50) -> list[dict]:
    """트렌딩 비디오 수집. 1유닛."""
    data = _get("videos", {
        "part": "snippet,statistics,contentDetails",
        "chart": "mostPopular",
        "regionCode": region,
        "videoCategoryId": category,
        "maxResults": str(max_results),
    })
    return [_normalize_video(it, source=f"trending-{category}") for it in data.get("items", [])]


def search_videos(query: str, days: int = 7, max_results: int = 25, order: str = "viewCount") -> list[dict]:
    """키워드 검색. 100유닛/회."""
    published_after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    data = _get("search", {
        "part": "snippet",
        "q": query,
        "type": "video",
        "regionCode": "KR",
        "relevanceLanguage": "ko",
        "publishedAfter": published_after,
        "order": order,
        "maxResults": str(max_results),
    })
    items = data.get("items", [])
    return [_normalize_search_item(it, query=query) for it in items]


def enrich_stats(items: list[dict]) -> list[dict]:
    """search 결과에 viewCount/likeCount/commentCount 보강. 1유닛/50건.

    라이브(스트리밍) 영상은 publishedAt이 '방송 종료' 시각이라 '방금 올라온 폭발'로
    오인된다. liveStreamingDetails.actualStartTime(실제 방송 시작 = 조회수 쌓이기 시작)이
    있으면 published_at을 그걸로 보정하고 is_live=True로 표시한다.
    """
    if not items:
        return items
    by_id: dict[str, dict] = {it["video_id"]: it for it in items if it.get("video_id")}
    if not by_id:
        return items
    ids = list(by_id.keys())
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            data = _get("videos", {
                "part": "statistics,contentDetails,liveStreamingDetails",
                "id": ",".join(chunk),
            })
        except Exception as e:
            print(f"[yt] enrich 실패: {e}", file=sys.stderr)
            continue
        for v in data.get("items", []):
            vid = v.get("id", "")
            if vid in by_id:
                stats = v.get("statistics", {}) or {}
                by_id[vid]["view_count"] = int(stats.get("viewCount", 0) or 0)
                by_id[vid]["like_count"] = int(stats.get("likeCount", 0) or 0)
                by_id[vid]["comment_count"] = int(stats.get("commentCount", 0) or 0)
                by_id[vid]["duration"] = (v.get("contentDetails", {}) or {}).get("duration", "")
                by_id[vid]["duration_sec"] = parse_duration_seconds(by_id[vid]["duration"])
                by_id[vid]["format"] = classify_format(by_id[vid])
                by_id[vid]["is_short"] = by_id[vid]["format"] == "short"
                # 라이브 보정: 실제 방송 시작 시각을 게시일로
                live = v.get("liveStreamingDetails", {}) or {}
                start = live.get("actualStartTime")
                if start:
                    by_id[vid]["is_live"] = True
                    by_id[vid]["published_at"] = start  # 조회수 쌓이기 시작한 실제 시점
    return list(by_id.values())


def enrich_channel_stats(items: list[dict]) -> list[dict]:
    """영상 리스트의 channel_id를 모아 channels.list로 구독자/총영상수/채널생성일 부착.

    1유닛/50채널. 결과는 각 영상에 ``channel_subscriber_count``, ``channel_video_count``,
    ``channel_published_at`` 필드를 추가.
    """
    if not items:
        return items
    cids = sorted({(it.get("channel_id") or "") for it in items if it.get("channel_id")})
    cache: dict[str, dict] = {}
    for i in range(0, len(cids), 50):
        chunk = cids[i:i + 50]
        try:
            data = _get("channels", {
                "part": "statistics,snippet",
                "id": ",".join(chunk),
            })
        except Exception as e:
            print(f"[yt] channel enrich 실패: {e}", file=sys.stderr)
            continue
        for c in data.get("items", []):
            cid = c.get("id", "")
            stats = c.get("statistics", {}) or {}
            sn = c.get("snippet", {}) or {}
            cache[cid] = {
                "subs": int(stats.get("subscriberCount", 0) or 0),
                "videos": int(stats.get("videoCount", 0) or 0),
                "channel_published_at": sn.get("publishedAt", ""),
                "hidden_subs": bool(stats.get("hiddenSubscriberCount", False)),
            }
    for it in items:
        info = cache.get(it.get("channel_id") or "")
        if info:
            it["channel_subscriber_count"] = info["subs"]
            it["channel_video_count"] = info["videos"]
            it["channel_published_at"] = info["channel_published_at"]
            it["channel_subs_hidden"] = info["hidden_subs"]
        else:
            it.setdefault("channel_subscriber_count", 0)
            it.setdefault("channel_video_count", 0)
    return items


def score_copy_value(item: dict) -> dict:
    """카피 가치 점수. 신생/중형 양산형의 폭발 영상일수록 높음.

    반환: {"score": int, "breakdown": {axis: pts, ...}}
    """
    subs = int(item.get("channel_subscriber_count", 0) or 0)
    views = int(item.get("view_count", 0) or 0)
    likes = int(item.get("like_count", 0) or 0)
    comments = int(item.get("comment_count", 0) or 0)
    vcount = int(item.get("channel_video_count", 0) or 0)

    bd: dict[str, int] = {}

    # 1. 구독자 밴드 (작을수록 가산)
    if subs <= 0:
        bd["subscriber_band"] = 0
    elif subs < 10_000:
        bd["subscriber_band"] = 3
    elif subs < 100_000:
        bd["subscriber_band"] = 2
    elif subs < 300_000:
        bd["subscriber_band"] = 1
    elif subs < 500_000:
        bd["subscriber_band"] = 0
    else:
        bd["subscriber_band"] = -2

    # 2. 조회수/구독자 비율 (알고리즘 폭발)
    ratio = (views / subs) if subs > 0 else 0
    if ratio >= 10:
        bd["view_to_sub"] = 3
    elif ratio >= 3:
        bd["view_to_sub"] = 2
    elif ratio >= 1:
        bd["view_to_sub"] = 1
    else:
        bd["view_to_sub"] = 0

    # 3. 채널 영상 수 (양산형 vs 신생)
    if 0 < vcount < 50:
        bd["channel_age"] = 1
    elif vcount >= 200:
        bd["channel_age"] = 0
    else:
        bd["channel_age"] = 0

    # 4. 참여도 = (likes + comments) / views. 너무 낮으면 봇/광고 의심
    eng = ((likes + comments) / views) if views > 0 else 0
    if eng >= 0.005:  # 0.5%
        bd["engagement"] = 1
    elif eng < 0.0005 and views > 10000:  # 0.05% 이하 + 큰 조회수
        bd["engagement"] = -1
    else:
        bd["engagement"] = 0

    score = sum(bd.values())
    return {"score": score, "breakdown": bd, "view_to_sub_ratio": round(ratio, 2)}


def collect_for_fronts(fronts: list[dict], days: int = 7, per_keyword: int = 25, max_keywords_per_front: int = 2) -> list[dict]:
    """9개 분야 × 키워드 N개씩 검색."""
    all_items: list[dict] = []
    for f in fronts:
        kws = (list(f.get("keywords_ko", []) or []) + list(f.get("keywords_en", []) or []))[:max_keywords_per_front]
        for kw in kws:
            try:
                items = search_videos(kw, days=days, max_results=per_keyword)
                for it in items:
                    it["front_id"] = f.get("id", "")
                    it["matched_keyword"] = kw
                all_items.extend(items)
                print(f"[yt] 검색 {f.get('id','')}/{kw}: {len(items)}건", file=sys.stderr)
            except Exception as e:
                print(f"[yt] 검색 {kw} 실패: {e}", file=sys.stderr)
    return all_items


def _uploads_playlist_id(channel_id: str) -> str:
    """채널 ID(UC...)의 '업로드 전체' 재생목록 ID(UU...). 앞 2글자만 UC→UU."""
    cid = (channel_id or "").strip()
    if cid.startswith("UC") and len(cid) >= 2:
        return "UU" + cid[2:]
    return ""


def _normalize_playlist_item(item: dict) -> dict:
    """playlistItems.list 응답 → 표준 비디오 dict (_normalize_search_item과 동일 스키마)."""
    sn = item.get("snippet", {}) or {}
    rid = (sn.get("resourceId", {}) or {})
    vid = rid.get("videoId", "")
    return {
        "video_id": vid,
        "title": html.unescape(sn.get("title", "")),
        "description": html.unescape((sn.get("description", "") or ""))[:500],
        "channel_id": sn.get("videoOwnerChannelId", "") or sn.get("channelId", ""),
        "channel_title": html.unescape(sn.get("videoOwnerChannelTitle", "") or sn.get("channelTitle", "")),
        "published_at": sn.get("publishedAt", ""),
        "thumbnail": (sn.get("thumbnails", {}) or {}).get("medium", {}).get("url", ""),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "view_count": 0, "like_count": 0, "comment_count": 0,
        "duration": "", "duration_sec": 0, "format": "unknown", "is_short": False,
        "matched_keyword": "", "source": "",
    }


def _collect_channel_popular(ch: dict, days: int, max_results: int) -> list[dict]:
    """뉴스 채널용: search.list(channelId, order=viewCount)로 조회수 상위만. 100유닛/채널.

    뉴스는 하루 수백 개 올려 playlistItems(최신순)로는 떡상이 밀린다. 조회수순으로 뽑으면
    하루 500개 올려도 그중 잘 나가는 것만 정확히 잡는다.
    """
    cid = ch.get("channel_id", "")
    cname = ch.get("name", "")
    after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z")
    try:
        data = _get("search", {
            "part": "snippet", "channelId": cid, "type": "video",
            "order": "viewCount", "publishedAfter": after,
            "maxResults": str(min(max_results, 50)),
        })
        items = [_normalize_search_item(it, query="") for it in data.get("items", [])]
        for it in items:
            it["whitelist_name"] = cname
            it["source"] = f"newsradar-{cid}"
        print(f"[newsradar] {cname}: {len(items)}건 (popular)", file=sys.stderr)
        return items
    except Exception as e:
        print(f"[newsradar] {cname}({cid}) popular 수집 실패: {e}", file=sys.stderr)
        return []


def collect_channel_uploads(channel_ids: list[dict], days: int = 7, max_results: int = 30,
                            mode: str = "recent") -> list[dict]:
    """채널들의 영상 수집 (떡상 감시기용).

    mode="recent"(기본): playlistItems로 최신 업로드. 1유닛/채널. 양산형(업로드 드묾)에 적합.
    mode="popular": search order=viewCount로 조회수 상위. 100유닛/채널. 뉴스(폭발적 업로드)에 적합.
    조회수는 이후 enrich_stats(videos.list)가 보강.
    channel_ids: [{"name": ..., "channel_id": ...}, ...]
    """
    if mode == "popular":
        out: list[dict] = []
        for ch in channel_ids:
            if ch.get("channel_id"):
                out.extend(_collect_channel_popular(ch, days, max_results))
        return out
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    all_items: list[dict] = []
    for ch in channel_ids:
        cid = ch.get("channel_id", "")
        cname = ch.get("name", "")
        pl = _uploads_playlist_id(cid)
        if not pl:
            print(f"[newsradar] {cname}: 잘못된 channel_id({cid}) 스킵", file=sys.stderr)
            continue
        # 페이지네이션: max_results까지 50개씩 여러 페이지. days 윈도우 벗어나면 조기 중단.
        # 뉴스 채널은 하루 수백 개 올려 30개가 반나절치라, max_results를 크게 잡아 페이지로 긁는다.
        items = []
        token = None
        pages = 0
        max_pages = max(1, (max_results + 49) // 50)
        stop = False
        try:
            while pages < max_pages and not stop:
                params = {"part": "snippet", "playlistId": pl, "maxResults": "50"}
                if token:
                    params["pageToken"] = token
                data = _get("playlistItems", params)
                pages += 1
                for it in data.get("items", []):
                    v = _normalize_playlist_item(it)
                    if not v["video_id"]:
                        continue
                    pub = v["published_at"]
                    if pub:
                        try:
                            if datetime.fromisoformat(pub.replace("Z", "+00:00")) < cutoff:
                                # 최신순이라 여기부터는 다 오래됨 → 이 채널 조기 종료
                                stop = True
                                break
                        except ValueError:
                            pass
                    v["whitelist_name"] = cname
                    v["source"] = f"newsradar-{cid}"
                    items.append(v)
                    if len(items) >= max_results:
                        stop = True
                        break
                token = data.get("nextPageToken")
                if not token:
                    break
            all_items.extend(items)
            print(f"[newsradar] {cname}: {len(items)}건 ({pages}p)", file=sys.stderr)
        except Exception as e:
            print(f"[newsradar] {cname}({cid}) 수집 실패: {e}", file=sys.stderr)
    return all_items


def resolve_channel(url_or_query: str) -> dict | None:
    """채널 URL 또는 검색어 → {channel_id, title, subs}. 등록용.

    지원 형태:
      https://youtube.com/channel/UCxxxx        → 그대로 channel_id
      https://youtube.com/@handle               → channels.list(forHandle)
      https://youtube.com/c/custom, /user/name  → search로 근사
      그냥 채널명 텍스트                          → search로 근사(구독자 최다)
    """
    s = (url_or_query or "").strip()
    if not s:
        return None
    import re as _re
    import urllib.parse as _up
    # 퍼센트 인코딩된 URL(한글 핸들 등) 디코딩 — @%EB%B6%80... → @부자의경제학
    try:
        s = _up.unquote(s)
    except Exception:
        pass

    # 1) /channel/UC... — 직접
    m = _re.search(r"/channel/(UC[\w-]{20,})", s)
    if m:
        info = get_channel_info([m.group(1)])
        return info[0] if info else None

    # 2) @handle (URL 또는 @핸들 텍스트). 한글·영숫자·._- 허용
    handle = None
    mh = _re.search(r"/@([^/?\s]+)", s)
    if mh:
        handle = mh.group(1)
    elif s.startswith("@"):
        handle = s[1:]
    if handle:
        try:
            data = _get("channels", {"part": "snippet,statistics", "forHandle": handle})
            items = data.get("items", [])
            if items:
                it = items[0]
                st = it.get("statistics", {}) or {}
                return {"channel_id": it.get("id", ""),
                        "title": (it.get("snippet", {}) or {}).get("title", ""),
                        "subs": int(st.get("subscriberCount", 0) or 0)}
        except Exception:
            pass  # 폴백: 아래 search

    # 3) /c/, /user/, 커스텀 — 마지막 경로 조각을 검색어로
    mc = _re.search(r"/(?:c|user)/([\w.\-가-힣]+)", s)
    query = mc.group(1) if mc else s
    cands = search_channels(query, max_results=3)
    return cands[0] if cands else None


def search_channels(query: str, max_results: int = 5) -> list[dict]:
    """채널명으로 채널 검색 → 구독자수 보강 → 구독자 내림차순. (add-channel용)

    반환: [{channel_id, title, subs}, ...]  100유닛.
    """
    data = _get("search", {
        "part": "snippet",
        "q": query,
        "type": "channel",
        "regionCode": "KR",
        "relevanceLanguage": "ko",
        "maxResults": str(max_results),
    })
    cands = [{
        "channel_id": it["snippet"]["channelId"],
        "title": it["snippet"].get("title", ""),
    } for it in data.get("items", [])]
    info = get_channel_info([c["channel_id"] for c in cands])
    subs = {x["channel_id"]: x.get("subs", 0) for x in info}
    for c in cands:
        c["subs"] = subs.get(c["channel_id"], 0)
    cands.sort(key=lambda c: c.get("subs", 0), reverse=True)
    return cands


def get_channel_info(channel_ids: list[str]) -> list[dict]:
    """channel_id 목록 → 제목/구독자수. (add-channel 검증·표시용) 1유닛/50건."""
    ids = [c for c in channel_ids if c]
    if not ids:
        return []
    out: list[dict] = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = _get("channels", {"part": "snippet,statistics", "id": ",".join(chunk)})
        for it in data.get("items", []):
            st = it.get("statistics", {}) or {}
            out.append({
                "channel_id": it.get("id", ""),
                "title": (it.get("snippet", {}) or {}).get("title", ""),
                "subs": int(st.get("subscriberCount", 0) or 0),
            })
    return out


def _normalize_video(item: dict, source: str = "") -> dict:
    sn = item.get("snippet", {}) or {}
    st = item.get("statistics", {}) or {}
    out = {
        "video_id": item.get("id", ""),
        "title": html.unescape(sn.get("title", "")),
        "description": html.unescape((sn.get("description", "") or ""))[:500],
        "channel_id": sn.get("channelId", ""),
        "channel_title": html.unescape(sn.get("channelTitle", "")),
        "published_at": sn.get("publishedAt", ""),
        "thumbnail": (sn.get("thumbnails", {}) or {}).get("medium", {}).get("url", ""),
        "category_id": sn.get("categoryId", ""),
        "tags": (sn.get("tags", []) or [])[:10],
        "view_count": int(st.get("viewCount", 0) or 0),
        "like_count": int(st.get("likeCount", 0) or 0),
        "comment_count": int(st.get("commentCount", 0) or 0),
        "duration": (item.get("contentDetails", {}) or {}).get("duration", ""),
        "url": f"https://www.youtube.com/watch?v={item.get('id','')}",
        "source": source,
    }
    out["duration_sec"] = parse_duration_seconds(out["duration"])
    out["format"] = classify_format(out)
    out["is_short"] = out["format"] == "short"
    return out


def _normalize_search_item(item: dict, query: str = "") -> dict:
    sn = item.get("snippet", {}) or {}
    vid = (item.get("id", {}) or {}).get("videoId", "")
    return {
        "video_id": vid,
        "title": html.unescape(sn.get("title", "")),
        "description": html.unescape((sn.get("description", "") or ""))[:500],
        "channel_id": sn.get("channelId", ""),
        "channel_title": html.unescape(sn.get("channelTitle", "")),
        "published_at": sn.get("publishedAt", ""),
        "thumbnail": (sn.get("thumbnails", {}) or {}).get("medium", {}).get("url", ""),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "view_count": 0,
        "like_count": 0,
        "comment_count": 0,
        "duration": "",
        "duration_sec": 0,
        "format": "unknown",
        "is_short": False,
        "matched_keyword": query,
        "source": f"search-{query}",
    }
