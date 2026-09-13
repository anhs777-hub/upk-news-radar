"""뉴스 레이더 메인 CLI.

서브커맨드: collect / filter / analyze / run / serve / promote / archive
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def _no_window_kwargs() -> dict:
    """Windows에서 자식 프로세스가 콘솔 창을 띄우지 않게 한다."""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}

# Windows 콘솔(cp949) 직접 실행 시 한글/특수문자(— 등) 출력이 죽는 것 방지.
# server.py가 subprocess(encoding=utf-8)로 부를 땐 무관하지만, CLI 직접 실행 보호.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import store, rss, filter as fil, llm, render  # noqa: E402

import json  # noqa: E402

ENV_PATH = ROOT / ".env"
DEFAULT_CHANNEL = "science"


class Paths:
    """채널별 경로 묶음. radar.py 안에서 모든 IO는 이 객체를 거친다."""
    def __init__(self, channel: str):
        self.channel = channel
        self.data_dir = ROOT / "data" / channel
        self.raw_dir = self.data_dir / "_raw"
        self.filtered_dir = self.data_dir / "_filtered"
        self.daily_dir = self.data_dir / "daily"
        self.candidates = self.data_dir / "candidates.json"
        self.archived = self.data_dir / "archived.json"
        self.yt_daily_dir = self.data_dir / "youtube" / "daily"
        self.yt_raw_dir = self.data_dir / "youtube" / "_raw"
        self.state_path = self.data_dir / "_radarstate.json"
        self.config_path = ROOT / "config" / f"{channel}.json"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _paths(args: argparse.Namespace) -> Paths:
    return Paths(getattr(args, "channel", None) or DEFAULT_CHANNEL)


def _load_config(p: Paths) -> dict:
    cfg = store.read_json(p.config_path)
    if not cfg:
        print(f"[radar] config 없음: {p.config_path}", file=sys.stderr)
        sys.exit(2)
    return cfg


def cmd_collect(args: argparse.Namespace) -> None:
    P = _paths(args)
    cfg = _load_config(P)
    date = args.date or _today()
    print(f"[collect] [{P.channel}] {date}")

    # 동적 윈도우 계산 (증분 모드, 채널별 state)
    state = store.read_state(P.state_path)
    now = datetime.now(timezone.utc)
    cutoff, note = fil.compute_window(now, state)
    hours = (now - cutoff).total_seconds() / 3600
    print(f"[window] {note} (cutoff={cutoff.isoformat()})")

    items = rss.fetch_all(cfg.get("sources", []))

    # 24h 넘는 윈도우 → 구글 뉴스 시간 검색 보강
    if hours > 24:
        days = min(14, max(2, int(hours / 24) + 1))
        print(f"[recovery] {hours:.0f}h 윈도우 → 구글 뉴스 시간 검색 보강 ({days}d)")
        bonus = rss.collect_recovery(cfg.get("fronts", []), days)
        items.extend(bonus)
        print(f"[recovery] 보강 {len(bonus)}건 추가")

    out = P.raw_dir / f"{date}.jsonl"
    n = store.write_jsonl(out, items)
    print(f"[collect] {n}건 → {out}")


def cmd_filter(args: argparse.Namespace) -> None:
    P = _paths(args)
    cfg = _load_config(P)
    date = args.date or _today()
    src = P.raw_dir / f"{date}.jsonl"
    items = store.read_jsonl(src)
    if not items:
        print(f"[filter] 원본 비어 있음: {src}", file=sys.stderr)
    fronts = cfg.get("fronts", [])
    cfg_filter = cfg.get("filter", {})
    # 증분 모드: hours=None이면 filter_items가 compute_window를 다시 계산 (멱등)
    filtered = fil.filter_items(items, fronts, cfg_filter, hours=None, state_path=P.state_path)
    max_items = int(cfg_filter.get("max_items", 100))
    capped = fil.rank_and_cap(filtered, fronts, max_items=max_items)
    out = P.filtered_dir / f"{date}.jsonl"
    n = store.write_jsonl(out, capped)
    print(f"[filter] {len(items)} → {len(filtered)} (키워드/시간) → {n} (상위 {max_items}) → {out}")


def cmd_analyze(args: argparse.Namespace) -> None:
    P = _paths(args)
    cfg = _load_config(P)
    llm.load_env(ENV_PATH)
    date = args.date or _today()
    src = P.filtered_dir / f"{date}.jsonl"
    items = store.read_jsonl(src)
    raw_count = len(store.read_jsonl(P.raw_dir / f"{date}.jsonl"))
    if not items:
        print(f"[analyze] 필터본 비어 있음: {src}", file=sys.stderr)
        return
    fronts = cfg.get("fronts", [])
    axes = cfg["scoring"]["axes"]
    th_cand = float(cfg["scoring"]["threshold_candidate"])
    th_watch = float(cfg["scoring"]["threshold_watch"])
    ctr_cfg = cfg.get("ctr_scoring", {}) or {}
    ctr_axes_def = ctr_cfg.get("axes", []) or []
    ctr_thresholds = ctr_cfg.get("thresholds", {}) or {}

    if getattr(args, "dry_run", False):
        expected_cand = max(1, len(items) // 20)
        est_chars = len(items) * 250 + expected_cand * 1500
        print(f"[dry-run] LLM에 {len(items)}건 넘김, 예상 토큰 약 {est_chars}자 "
              f"(카피 예상 {expected_cand}건). 실제 호출은 하지 않음.")
        return

    print(f"[analyze] {len(items)}건 → claude CLI로 분류·점수화")
    enriched = llm.classify_and_score(items, fronts, axes)

    # 후보 임계 통과 항목 → 전선별 쿼터 적용 → 카피 생성
    front_by_id = {f["id"]: f for f in fronts}
    quota = int(cfg["scoring"].get("candidate_quota_per_front", 0))  # 0=무제한

    passed = [it for it in enriched if it.get("total", 0) >= th_cand and it.get("front_id")]
    passed.sort(key=lambda x: x.get("total", 0), reverse=True)

    if quota > 0:
        per_front: dict[str, int] = {}
        selected_ids = set()
        dropped_by_front: dict[str, int] = {}
        for it in passed:
            fid = it["front_id"]
            if per_front.get(fid, 0) < quota:
                per_front[fid] = per_front.get(fid, 0) + 1
                selected_ids.add(it.get("id"))
            else:
                dropped_by_front[fid] = dropped_by_front.get(fid, 0) + 1
        if dropped_by_front:
            drops = ", ".join(f"{fid}:{n}" for fid, n in dropped_by_front.items())
            print(f"[analyze] 쿼터({quota}/전선) 초과로 카피 제외: {drops}")
    else:
        selected_ids = {it.get("id") for it in passed}

    cand_count = 0
    for it in enriched:
        if it.get("id") in selected_ids:
            front = front_by_id.get(it["front_id"], {})
            print(f"[analyze] 카피 생성: {it.get('title','')[:40]}")
            it["copy"] = llm.generate_copy(
                it, front,
                ctr_axes_def=ctr_axes_def,
                ctr_thresholds=ctr_thresholds,
            )
            cand_count += 1
        else:
            it["copy"] = {}

    daily = render.build_daily(
        date=date,
        raw_count=raw_count,
        filtered_count=len(items),
        enriched=enriched,
        fronts=fronts,
        threshold_candidate=th_cand,
        threshold_watch=th_watch,
    )
    out = render.save_daily(P.data_dir, daily)
    print(f"[analyze] 후보 {cand_count}건 / 저장 → {out}")


def cmd_run(args: argparse.Namespace) -> None:
    P = _paths(args)
    cmd_collect(args)
    cmd_filter(args)
    cmd_analyze(args)
    # analyze 성공 후 last_run_at 갱신 (다음 실행의 증분 cutoff 기준)
    if not getattr(args, "dry_run", False):
        store.update_last_run(path=P.state_path)
        print("[run] last_run_at 갱신됨")


def cmd_ytrun(args: argparse.Namespace) -> None:
    """YouTube 레이더 1회 실행: 트렌딩 + 분야별 검색 → LLM 빈 자리 분석."""
    P = _paths(args)
    cfg = _load_config(P)
    llm.load_env(ENV_PATH)
    from src import youtube as yt, ytanalyze
    print("[ytrun] YouTube 트렌딩 + 분야별 검색 수집")

    fronts = cfg.get("fronts", []) or []

    # 1. 트렌딩 (채널별 카테고리 설정 가능, 각 1유닛)
    yt_cfg = cfg.get("youtube", {}) or {}
    trending_cats = yt_cfg.get("trending_categories") or [yt.CATEGORY_SCIENCE, yt.CATEGORY_EDUCATION]
    trending: list[dict] = []
    for cat in trending_cats:
        try:
            t = yt.collect_trending(category=cat, max_results=50)
            for item in t:
                item["front_id"] = ""
                item["matched_keyword"] = f"trending-{cat}"
            trending.extend(t)
            print(f"[yt] 트렌딩 {cat}: {len(t)}건")
        except Exception as e:
            print(f"[yt] 트렌딩 {cat} 실패: {e}", file=sys.stderr)

    # 2. 분야별 키워드 검색
    print("[yt] 분야별 키워드 검색 시작...")
    try:
        search_items = yt.collect_for_fronts(fronts, days=7, per_keyword=25, max_keywords_per_front=2)
    except Exception as e:
        print(f"[yt] 검색 실패: {e}", file=sys.stderr)
        search_items = []
    print(f"[yt] 검색 합계: {len(search_items)}건")

    # 3. 통계 보강
    print("[yt] 통계 보강 중...")
    try:
        enriched = yt.enrich_stats(search_items)
    except Exception as e:
        print(f"[yt] 통계 보강 실패: {e}", file=sys.stderr)
        enriched = search_items

    # 3b. 채널 통계 보강 (구독자/총영상수)
    print("[yt] 채널 통계 보강 중...")
    try:
        yt.enrich_channel_stats(trending + enriched)
    except Exception as e:
        print(f"[yt] 채널 통계 보강 실패: {e}", file=sys.stderr)

    # 3c. 카피 가치 점수
    for it in trending + enriched:
        try:
            cs = yt.score_copy_value(it)
            it["copy_score"] = cs["score"]
            it["copy_score_breakdown"] = cs["breakdown"]
            it["view_to_sub_ratio"] = cs["view_to_sub_ratio"]
        except Exception:
            it["copy_score"] = 0

    # 4. 합치기 + 중복 제거
    all_items = trending + enriched
    seen = set()
    deduped: list[dict] = []
    for it in all_items:
        vid = it.get("video_id")
        if vid and vid not in seen:
            seen.add(vid)
            deduped.append(it)
    print(f"[yt] 중복 제거 후: {len(deduped)}건")

    # 5. 분야별 그룹화
    by_front: dict[str, list[dict]] = {}
    for it in deduped:
        fid = it.get("front_id") or "_uncategorized"
        by_front.setdefault(fid, []).append(it)

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if getattr(args, "dry_run", False):
        n_fronts = len([k for k in by_front if k != "_uncategorized" and by_front[k]])
        est_tokens = n_fronts * 3500
        print(f"[dry-run] 분야 {n_fronts}개 분석, 예상 토큰 약 {est_tokens}자. 실제 LLM 호출은 하지 않음.")
        P.yt_raw_dir.mkdir(parents=True, exist_ok=True)
        store.write_jsonl(P.yt_raw_dir / f"{date}.jsonl", deduped)
        return

    # 6. LLM 분석
    print(f"[ytanalyze] {len(by_front)}개 분야 분석 시작 (claude CLI)")
    analyses = ytanalyze.analyze_all_fronts(by_front, fronts)
    print(f"[ytanalyze] 분석 완료: {len(analyses)}건")

    # 6b. 최근 3일치 RSS와 임베딩 매칭 → 분야별 related_rss 부착
    try:
        from src import embed as _embed
        rss_items = _embed.load_rss_items_window(P.daily_dir, date, days=3)
        if rss_items:
            print(f"[match] RSS {len(rss_items)}건(최근 3일) 임베딩 매칭 (BGE-M3)")
            n = _embed.attach_related_rss(analyses, rss_items, top_k=5)
            print(f"[match] 분야별 관련 RSS 부착 완료: 총 {n}건")
        else:
            print(f"[match] 최근 3일치 RSS daily 없음 — 스킵")
    except Exception as e:
        print(f"[match] 매칭 실패(무시): {e}", file=sys.stderr)

    # 7. 저장
    out = {
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "trending_count": len(trending),
            "search_count": len(search_items),
            "deduped_count": len(deduped),
            "fronts_analyzed": len(analyses),
            "long_count": sum(1 for v in deduped if v.get("format") == "long"),
            "short_count": sum(1 for v in deduped if v.get("format") == "short"),
            "ambiguous_count": 0,  # 3분 경계 2분류화 이후 항상 0 (스키마 호환용)
            "high_copy_value_count": sum(1 for v in deduped if (v.get("copy_score") or 0) >= 4),
        },
        "analyses": analyses,
        "videos": deduped,
    }
    P.yt_daily_dir.mkdir(parents=True, exist_ok=True)
    out_path = P.yt_daily_dir / f"{date}.json"
    store.write_json(out_path, out)

    # index.json 갱신
    idx_path = P.yt_daily_dir / "index.json"
    existing: list[str] = []
    if idx_path.exists():
        try:
            existing = json.loads(idx_path.read_text(encoding="utf-8")).get("dates", []) or []
        except Exception:
            existing = []
    if date not in existing:
        existing.append(date)
        existing.sort(reverse=True)
    store.write_json(idx_path, {"dates": existing})

    print(f"[ytrun] 완료 → {out_path}")


# ---------------------------------------------------------------------------
# 언론사 떡상 감시기 (newsradar) — 채널 결과 무관 공용 트랙
# config/_breakout.json (whitelist + breakout), data/_newsradar/ 에 저장
# ---------------------------------------------------------------------------
BREAKOUT_CONFIG = ROOT / "config" / "_breakout.json"
NEWSRADAR_DIR = ROOT / "data" / "_newsradar"


def _load_breakout_config() -> dict:
    cfg = store.read_json(BREAKOUT_CONFIG)
    if not cfg:
        print(f"[newsradar] config 없음: {BREAKOUT_CONFIG}", file=sys.stderr)
        sys.exit(2)
    return cfg


def _categories(cfg: dict) -> list[dict]:
    return cfg.get("categories", []) or []


def _find_category(cfg: dict, cat_id: str) -> dict | None:
    return next((c for c in _categories(cfg) if c.get("id") == cat_id), None)


def _merged_breakout(cfg: dict, cat: dict) -> dict:
    """defaults + 분류별 breakout 병합 (분류값 우선)."""
    merged = dict(cfg.get("defaults", {}) or {})
    merged.update(cat.get("breakout", {}) or {})
    return merged


def _run_newsradar_category(cfg: dict, cat: dict, date: str, now, dry_run: bool = False) -> None:
    """한 분류에 대해 수집→보강→추적→분류→저장. 저장은 data/_newsradar/<id>/ 에."""
    from src import youtube as yt, breakout as bo

    cat_id = cat.get("id", "")
    cat_name = cat.get("name", cat_id)
    channels = cat.get("channels", []) or []
    bcfg = _merged_breakout(cfg, cat)
    if not channels:
        print(f"[newsradar:{cat_id}] 채널 비어 있음 — 스킵", file=sys.stderr)
        return

    days = int(bcfg.get("collect_days", 14))
    max_per = int(bcfg.get("max_results_per_channel", 30))
    threshold = int(bcfg.get("track_threshold", 100_000))
    mode = str(bcfg.get("collect_mode", "recent"))
    cat_dir = NEWSRADAR_DIR / cat_id

    print(f"[newsradar:{cat_id}] {date} — {cat_name} {len(channels)}채널, 최근 {days}일 수집 (mode={mode})")
    items = yt.collect_channel_uploads(channels, days=days, max_results=max_per, mode=mode)
    print(f"[newsradar:{cat_id}] 수집 {len(items)}건")

    if dry_run:
        items = yt.enrich_stats(items)
        over = [it for it in items if int(it.get("view_count", 0) or 0) >= threshold]
        print(f"[dry-run:{cat_id}] {threshold:,}+ 도달 {len(over)}건 (저장 안 함)")
        return

    items = yt.enrich_stats(items)
    # 구독자수 보강 — 효율(조회수/구독자) 계산용. 50채널당 1유닛이라 비용 미미.
    # 속도지수(절대량)만으로는 대형채널의 일상 조회가 위로 올라온다. 체급 대비
    # 얼마나 퍼졌는지를 함께 봐야 '채널 힘'과 '콘텐츠 힘'이 구분된다.
    yt.enrich_channel_stats(items)

    track_path = cat_dir / "track.json"
    track = store.read_json(track_path, {}) or {}
    track = bo.update_track(track, items, date=date, threshold=threshold, now=now)
    store.write_json(track_path, track)

    leaderboard = bo.build_leaderboard(track, bcfg, now)
    tiers: dict[str, int] = {}
    for r in leaderboard:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1

    out = {
        "date": date,
        "category": cat_id,
        "category_name": cat_name,
        "generated_at": now.isoformat(),
        "config": bcfg,
        "channels": channels,
        "stats": {
            "collected": len(items),
            "tracked": len(track),
            "shown": len(leaderboard),
            "shorts_excluded": len(track) - len(leaderboard),
            "hot": tiers.get("hot", 0),
            "rising": tiers.get("rising", 0),
            "base": tiers.get("base", 0),
            "new": tiers.get("new", 0),
            "watch": tiers.get("watch", 0),
        },
        "leaderboard": leaderboard,
    }
    daily_dir = cat_dir / "daily"
    store.write_json(daily_dir / f"{date}.json", out)

    idx_path = daily_dir / "index.json"
    existing = (store.read_json(idx_path, {}) or {}).get("dates", []) or []
    if date not in existing:
        existing.append(date)
        existing.sort(reverse=True)
    store.write_json(idx_path, {"dates": existing})

    print(f"[newsradar:{cat_id}] 추적 {len(track)}건 / 🔥{tiers.get('hot',0)} 📈{tiers.get('rising',0)} "
          f"🧊{tiers.get('base',0)} ⏳{tiers.get('watch',0)}")


def cmd_newsradar(args: argparse.Namespace) -> None:
    """떡상 감시기 실행. --category 있으면 그 분류만, 없으면 전체 분류 순회.

    자동(스케줄러)=전체, 지금감지(수동)=단일 분류.
    분류별로 수집→보강→추적(시계열)→등급분류→저장(data/_newsradar/<id>/).
    """
    llm.load_env(ENV_PATH)
    cfg = _load_breakout_config()
    now = datetime.now(timezone.utc)
    date = args.date or now.strftime("%Y-%m-%d")
    dry = getattr(args, "dry_run", False)

    cat_id = getattr(args, "category", None)
    if cat_id:
        cat = _find_category(cfg, cat_id)
        if not cat:
            print(f"[newsradar] 분류 없음: {cat_id}", file=sys.stderr)
            sys.exit(2)
        _run_newsradar_category(cfg, cat, date, now, dry_run=dry)
    else:
        cats = _categories(cfg)
        if not cats:
            print("[newsradar] 분류 없음", file=sys.stderr)
            sys.exit(2)
        # 전체(자동) 순회는 auto_collect가 false가 아닌 분류만. (기본 true)
        active = [c for c in cats if c.get("auto_collect", True)]
        skipped = [c.get("name", c.get("id")) for c in cats if not c.get("auto_collect", True)]
        if skipped:
            print(f"[newsradar] 자동 제외 분류: {', '.join(skipped)}")
        print(f"[newsradar] 전체 {len(active)}개 분류 순회")
        for cat in active:
            _run_newsradar_category(cfg, cat, date, now, dry_run=dry)


def cmd_resolve_channel(args: argparse.Namespace) -> None:
    """채널 URL/핸들/검색어 → channel_id 해석해서 JSON으로 stdout 출력.

    server.py가 subprocess로 부른다(config 저장은 server가 담당). 마지막 줄에 JSON.
    """
    llm.load_env(ENV_PATH)
    from src import youtube as yt
    import json as _json
    q = args.query
    try:
        r = yt.resolve_channel(q)
    except Exception as e:
        print(_json.dumps({"ok": False, "error": str(e)[:150]}, ensure_ascii=False))
        return
    if not r or not r.get("channel_id"):
        print(_json.dumps({"ok": False, "error": "채널을 찾을 수 없음"}, ensure_ascii=False))
        return
    print(_json.dumps({"ok": True, "channel_id": r["channel_id"],
                       "name": r.get("title", ""), "subs": r.get("subs", 0)}, ensure_ascii=False))


def cmd_serve(args: argparse.Namespace) -> None:
    """단순 정적 서버. python http.server 래퍼."""
    import http.server
    import socketserver
    import os
    os.chdir(ROOT)
    port = int(args.port)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        print(f"[serve] http://localhost:{port}/index.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] 종료")


def _find_in_daily(P: Paths, item_id: str) -> dict | None:
    """RSS 또는 YT daily에서 id로 항목 조회.

    YT 항목 id 규약: ``yt:{date}:{front_id}`` — yt daily 파일의 analyses[].front_id 매칭.
    매칭 결과는 카드 뷰에 필요한 핵심 필드만 평탄화해서 반환.
    """
    if item_id.startswith("yt:"):
        try:
            _, date, front_id = item_id.split(":", 2)
        except ValueError:
            return None
        p = P.yt_daily_dir / f"{date}.json"
        d = store.read_json(p, {})
        for a in d.get("analyses", []):
            if a.get("front_id") == front_id:
                an = a.get("analysis", {}) or {}
                sv = an.get("suggested_video", {}) or {}
                return {
                    "id": item_id,
                    "kind": "youtube",
                    "date": date,
                    "front_id": front_id,
                    "front_name": a.get("front_name"),
                    "front_color": a.get("front_color"),
                    "title": sv.get("title") or a.get("front_name"),
                    "one_line_summary": sv.get("hook") or an.get("trend_summary"),
                    "suggested_video": sv,
                    "hot_topics": an.get("hot_topics", []),
                    "missing_angles": an.get("missing_angles", []),
                    "opportunity_tier": an.get("opportunity_tier"),
                }
        return None
    for p in sorted(P.daily_dir.glob("*.json"), reverse=True):
        if p.name == "index.json":
            continue
        d = store.read_json(p, {})
        for it in d.get("items", []):
            if it.get("id") == item_id:
                # 풀에서 카드로 점프하려면 어느 daily 파일에서 왔는지 알아야 함
                it["source_date"] = p.stem
                return it
    return None


def cmd_promote(args: argparse.Namespace) -> None:
    P = _paths(args)
    item_id = args.id
    item = _find_in_daily(P, item_id)
    if not item:
        print(f"[promote] id={item_id} 못 찾음", file=sys.stderr)
        sys.exit(1)
    pool = store.read_json(P.candidates, [])
    if any(x.get("id") == item_id for x in pool):
        print(f"[promote] 이미 후보 풀에 있음: {item_id}")
        return
    item["promoted_at"] = datetime.now(timezone.utc).isoformat()
    pool.append(item)
    store.write_json(P.candidates, pool)
    print(f"[promote] {item_id} → candidates.json ({len(pool)}건)")


def cmd_archive(args: argparse.Namespace) -> None:
    """후보 풀에 있으면 거기서 빼고, 없으면 daily에서 직접 찾아 archived로."""
    P = _paths(args)
    item_id = args.id
    pool = store.read_json(P.candidates, [])
    target = next((x for x in pool if x.get("id") == item_id), None)
    if target:
        pool = [x for x in pool if x.get("id") != item_id]
    else:
        target = _find_in_daily(P, item_id)
        if not target:
            print(f"[archive] id={item_id} 못 찾음", file=sys.stderr)
            sys.exit(1)
    archived = store.read_json(P.archived, [])
    if any(x.get("id") == item_id for x in archived):
        print(f"[archive] 이미 archived: {item_id}")
        store.write_json(P.candidates, pool)
        return
    target["archived_at"] = datetime.now(timezone.utc).isoformat()
    archived.append(target)
    store.write_json(P.candidates, pool)
    store.write_json(P.archived, archived)
    print(f"[archive] {item_id} → archived ({len(archived)}건)")


def cmd_ytmatch(args: argparse.Namespace) -> None:
    """기존 yt daily에 최근 N일치 RSS와의 임베딩 매칭 결과를 부착(후처리)."""
    P = _paths(args)
    date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    days = int(getattr(args, "days", 3) or 3)
    yt_path = P.yt_daily_dir / f"{date}.json"
    if not yt_path.exists():
        print(f"[ytmatch] yt daily 없음: {yt_path}", file=sys.stderr); sys.exit(1)
    yt_data = store.read_json(yt_path, {})
    from src import embed as _embed
    rss_items = _embed.load_rss_items_window(P.daily_dir, date, days=days)
    if not rss_items:
        print(f"[ytmatch] 최근 {days}일치 RSS daily 없음", file=sys.stderr); sys.exit(1)
    print(f"[ytmatch] RSS {len(rss_items)}건(최근 {days}일) 임베딩 매칭")
    n = _embed.attach_related_rss(yt_data.get("analyses", []), rss_items, top_k=5)
    store.write_json(yt_path, yt_data)
    print(f"[ytmatch] {date}: 관련 RSS {n}건 부착 → {yt_path}")


def cmd_unpromote(args: argparse.Namespace) -> None:
    """후보 풀(candidates.json)에서 항목 제거 (제외 풀로 보내지 않음)."""
    P = _paths(args)
    item_id = args.id
    pool = store.read_json(P.candidates, [])
    if not any(x.get("id") == item_id for x in pool):
        print(f"[unpromote] candidates에 id={item_id} 없음", file=sys.stderr)
        sys.exit(1)
    pool = [x for x in pool if x.get("id") != item_id]
    store.write_json(P.candidates, pool)
    print(f"[unpromote] {item_id} 후보 풀에서 제거 ({len(pool)}건 남음)")


def cmd_unarchive(args: argparse.Namespace) -> None:
    """제외 풀(archived.json)에서 항목 제거 = 복원."""
    P = _paths(args)
    item_id = args.id
    archived = store.read_json(P.archived, [])
    if not any(x.get("id") == item_id for x in archived):
        print(f"[unarchive] archived에 id={item_id} 없음", file=sys.stderr)
        sys.exit(1)
    archived = [x for x in archived if x.get("id") != item_id]
    store.write_json(P.archived, archived)
    print(f"[unarchive] {item_id} 복원 ({len(archived)}건 남음)")


def _shorten_query(q: str, max_len: int = 35) -> str:
    """기사/영상 제목 → 검색용 핵심 키워드로 축약 (언론사명·서술어 제거)."""
    import re as _re
    # 언론사명 제거 (- 서울신문, - 마켓인 등)
    q = _re.sub(r'\s*[-–—]\s*[가-힣A-Za-z]+(?:신문|일보|경제|투데이|뉴스|타임즈|인|넷)\s*$', '', q)
    # 따옴표, 괄호 등 제거
    q = _re.sub(r'["\'\(\)\[\]{}]', '', q)
    # 불필요한 서술어 제거
    q = _re.sub(r'(입니다|합니다|했습니다|됩니다|인가\??|에요\.?|거예요\.?|드나\??)\s*$', '', q.strip())
    q = _re.sub(r'\s+(때문|이유|방법|현실|전망|분석|정리)\s*$', '', q)
    if len(q) <= max_len:
        return q.strip()
    # 다양한 구분자로 분리해 앞부분만
    parts = _re.split(r'[,，.。·|?？!\-–—…]', q)
    short = parts[0].strip()
    if len(short) > max_len:
        words = short.split()
        short = ' '.join(words[:5])
    return short[:max_len].strip()


def _search_topic_refs(raw_query: str, days: int = 30, max_results: int = 15) -> list[dict]:
    """토픽 제목으로 같은 주제 유튜브 영상(레퍼런스 후보) 검색·정제·정렬.

    cand-search / breakout-refs 공용. 롱폼(>60초) + 조회수 1000+ 만, 조회수 내림차순.
    반환: 평탄화된 비디오 dict 리스트 (저장은 호출자 책임).
    """
    from src import youtube as yt
    query = _shorten_query(raw_query)
    print(f"[refs] raw={raw_query!r} → query={query!r}, days={days}, max={max_results}", file=sys.stderr)
    try:
        results = yt.search_videos(query, days=days, max_results=max_results, order="relevance")
        if results:
            results = yt.enrich_stats(results)
            results = [v for v in results
                       if v.get("duration_sec", 0) > 60 and v.get("view_count", 0) >= 1000]
            results.sort(key=lambda v: v.get("view_count", 0), reverse=True)
    except Exception as e:
        print(f"[refs] YouTube 검색 실패: {e}", file=sys.stderr)
        return []
    return [
        {
            "video_id": v["video_id"],
            "title": v["title"],
            "channel_title": v["channel_title"],
            "url": v["url"],
            "thumbnail": v["thumbnail"],
            "view_count": v.get("view_count", 0),
            "like_count": v.get("like_count", 0),
            "duration_sec": v.get("duration_sec", 0),
            "published_at": v.get("published_at", ""),
        }
        for v in results
    ]


def cmd_cand_search(args: argparse.Namespace) -> None:
    """후보 풀 항목의 키워드로 YouTube 검색, 결과를 candidates.json에 저장."""
    P = _paths(args)
    item_id = args.id
    pool = store.read_json(P.candidates, [])
    target = next((x for x in pool if x.get("id") == item_id), None)
    if not target:
        print(f"[cand-search] candidates에 id={item_id} 없음", file=sys.stderr)
        sys.exit(1)

    raw_query = target.get("title") or target.get("one_line_summary") or ""
    if not raw_query:
        print(f"[cand-search] 검색 쿼리 없음: {item_id}", file=sys.stderr)
        sys.exit(1)

    days = int(getattr(args, "days", 30) or 30)
    max_results = int(getattr(args, "max_results", 15) or 15)
    videos = _search_topic_refs(raw_query, days=days, max_results=max_results)
    print(f"[cand-search] {len(videos)}건 검색됨 (롱폼, 1000+뷰)")

    target["matched_videos"] = videos
    target["searched_at"] = datetime.now(timezone.utc).isoformat()
    store.write_json(P.candidates, pool)
    import json as _json
    print(_json.dumps({"count": len(videos), "videos": videos}, ensure_ascii=False))


def cmd_breakout_refs(args: argparse.Namespace) -> None:
    """떡상 영상의 토픽으로 추가 레퍼런스 영상 검색 (저장 없이 JSON 반환).

    뉴스 영상 1개로 맨땅 대본을 만들지 않도록, 같은 토픽 영상 여러 개를 모아
    레퍼런스 풀을 만든다. 프로젝트 생성은 하지 않고 결과만 반환 — 대본 생성은
    클립보드로 받은 URL 묶음을 script-pd가 분석·추가조사한다.
    """
    llm.load_env(ENV_PATH)
    vid = args.video_id
    # track.json에서 떡상 영상 제목 조회
    track = store.read_json(NEWSRADAR_DIR / "track.json", {}) or {}
    entry = track.get(vid)
    if not entry:
        print(f"[breakout-refs] track에 video_id={vid} 없음", file=sys.stderr)
        sys.exit(1)
    raw_query = entry.get("title") or ""
    if not raw_query:
        print(f"[breakout-refs] 제목 없음: {vid}", file=sys.stderr)
        sys.exit(1)

    days = int(getattr(args, "days", 30) or 30)
    max_results = int(getattr(args, "max_results", 15) or 15)
    videos = _search_topic_refs(raw_query, days=days, max_results=max_results)
    print(f"[breakout-refs] {len(videos)}건 검색됨", file=sys.stderr)
    import json as _json
    print(_json.dumps({
        "count": len(videos),
        "source": {"video_id": vid, "title": raw_query, "url": entry.get("url", "")},
        "videos": videos,
    }, ensure_ascii=False))


def cmd_cand_start(args: argparse.Namespace) -> None:
    """후보 항목의 레퍼런스 영상으로 프로젝트 생성 + collect.py 실행."""
    P = _paths(args)
    item_id = args.id
    pool = store.read_json(P.candidates, [])
    target = next((x for x in pool if x.get("id") == item_id), None)
    if not target:
        print(f"[cand-start] candidates에 id={item_id} 없음", file=sys.stderr)
        sys.exit(1)

    refs = target.get("selected_refs", [])
    matched = target.get("matched_videos", [])
    if not refs:
        print(f"[cand-start] 선택된 레퍼런스 없음: {item_id}", file=sys.stderr)
        sys.exit(1)

    # 레퍼런스 URL 목록 생성
    ref_urls = []
    for vid_id in refs:
        v = next((m for m in matched if m.get("video_id") == vid_id), None)
        if v:
            ref_urls.append(v["url"])
        else:
            ref_urls.append(f"https://www.youtube.com/watch?v={vid_id}")

    # 프로젝트명 생성: NN_mmdd_keyword
    import re as _re
    channel_id = P.channel
    # 실제 프로젝트는 repo 루트(ROOT.parent) 기준. collect.py cwd와 일치시켜야 한다.
    projects_dir = ROOT.parent / "channels" / channel_id / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    # 다음 순번 계산
    existing = sorted(projects_dir.iterdir()) if projects_dir.exists() else []
    max_nn = 0
    for d in existing:
        m = _re.match(r'^(\d+)_', d.name)
        if m:
            max_nn = max(max_nn, int(m.group(1)))
    nn = max_nn + 1

    # 키워드: 기사 제목에서 추출
    title = target.get("title") or target.get("one_line_summary") or "untitled"
    # 간단한 키워드 추출: 한글/영문 단어 중 2~6자 키워드 2~3개
    words = _re.findall(r'[가-힣A-Za-z]{2,}', title)
    # 불용어 제거
    stopwords = {'에서', '으로', '까지', '에게', '이란', '대한', '한국', '관련', '이번'}
    keywords = [w for w in words if w not in stopwords][:3]
    keyword_slug = '-'.join(keywords) if keywords else 'project'

    mmdd = datetime.now().strftime("%m%d")
    project_name = f"{nn:02d}_{mmdd}_{keyword_slug}"

    project_dir = projects_dir / project_name
    project_dir.mkdir(parents=True, exist_ok=True)

    # 기사 정보를 _refs/article.json으로 저장
    refs_dir = project_dir / "_refs"
    refs_dir.mkdir(exist_ok=True)
    import json as _json
    article_info = {
        "source_id": item_id,
        "title": target.get("title", ""),
        "one_line_summary": target.get("one_line_summary", ""),
        "link": target.get("link", ""),
        "source": target.get("source", ""),
        "front_id": target.get("front_id", ""),
        "front_name": target.get("front_name", ""),
        "total": target.get("total", 0),
        "copy": target.get("copy", {}),
        "selected_refs": refs,
        "ref_urls": ref_urls,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    store.write_json(refs_dir / "article.json", article_info)

    # collect.py 실행
    collect_script = ROOT.parent / "scripts" / "collect.py"
    python_exe = sys.executable
    cmd = [python_exe, str(collect_script), "--project", project_name, "--channel", channel_id] + ref_urls
    print(f"[cand-start] 프로젝트 생성: {project_name}")
    print(f"[cand-start] collect.py 실행: {len(ref_urls)}개 URL")
    print(f"[cand-start] cmd: {' '.join(cmd)}")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            **_no_window_kwargs(),
        )
        print(proc.stdout[-3000:] if proc.stdout else "")
        if proc.returncode != 0:
            print(f"[cand-start] collect.py 실패: {proc.stderr[-1000:]}", file=sys.stderr)
        else:
            print(f"[cand-start] ✓ 수집 완료")
    except subprocess.TimeoutExpired:
        print(f"[cand-start] collect.py 타임아웃 (300초)", file=sys.stderr)

    # 후보에 프로젝트 연결 정보 저장
    target["project_name"] = project_name
    target["project_started_at"] = datetime.now(timezone.utc).isoformat()
    store.write_json(P.candidates, pool)

    result = {"project_name": project_name, "channel": channel_id, "ref_count": len(ref_urls)}
    print(_json.dumps(result, ensure_ascii=False))


def cmd_cand_save_refs(args: argparse.Namespace) -> None:
    """후보 항목에 선택된 레퍼런스 영상 ID 목록 저장."""
    P = _paths(args)
    item_id = args.id
    pool = store.read_json(P.candidates, [])
    target = next((x for x in pool if x.get("id") == item_id), None)
    if not target:
        print(f"[cand-save-refs] candidates에 id={item_id} 없음", file=sys.stderr)
        sys.exit(1)
    import json as _json
    video_ids = _json.loads(args.video_ids)
    target["selected_refs"] = video_ids
    target["refs_saved_at"] = datetime.now(timezone.utc).isoformat()
    store.write_json(P.candidates, pool)
    print(f"[cand-save-refs] {item_id}: {len(video_ids)}개 레퍼런스 저장")


def _add_channel(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--channel", default=DEFAULT_CHANNEL,
                    help=f"채널 id (config/<id>.json). 기본 {DEFAULT_CHANNEL}")


def main() -> None:
    p = argparse.ArgumentParser(prog="radar", description="뉴스 레이더 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("collect", "filter", "analyze", "run"):
        sp = sub.add_parser(name)
        _add_channel(sp)
        sp.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 오늘 UTC)")
        if name in ("analyze", "run"):
            sp.add_argument("--dry-run", action="store_true",
                            help="LLM 호출 직전까지만 진행하고 예상 토큰만 출력")

    sp = sub.add_parser("serve")
    sp.add_argument("--port", default=8090)

    for name in ("promote", "archive", "unarchive", "unpromote"):
        sp = sub.add_parser(name)
        _add_channel(sp)
        sp.add_argument("--id", required=True)

    sp = sub.add_parser("cand-search", help="후보 항목의 키워드로 YouTube 검색")
    _add_channel(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--days", type=int, default=30, help="검색 범위 일수 (기본 30)")
    sp.add_argument("--max-results", type=int, default=15)

    sp = sub.add_parser("cand-save-refs", help="후보 항목에 레퍼런스 영상 저장")
    _add_channel(sp)
    sp.add_argument("--id", required=True)
    sp.add_argument("--video-ids", required=True, help="JSON 배열 문자열")

    sp = sub.add_parser("cand-start", help="후보 항목으로 프로젝트 생성 + 레퍼런스 수집")
    _add_channel(sp)
    sp.add_argument("--id", required=True)

    sp = sub.add_parser("ytmatch", help="기존 yt daily에 RSS 매칭 부착")
    _add_channel(sp)
    sp.add_argument("--date", default=None)
    sp.add_argument("--days", type=int, default=3, help="매칭할 RSS 윈도우 일수 (기본 3)")

    sp = sub.add_parser("ytrun", help="YouTube 레이더 1회 실행")
    _add_channel(sp)
    sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("newsradar", help="떡상 감시기 실행 (--category 없으면 전체 분류)")
    sp.add_argument("--category", default=None, help="분류 id (없으면 전체 분류 순회)")
    sp.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 오늘 UTC)")
    sp.add_argument("--dry-run", action="store_true",
                    help="수집·통계 보강까지만, 추적 풀 저장 없이 임계 통과분 미리보기")

    sp = sub.add_parser("resolve-channel", help="채널 URL/핸들/검색어 → channel_id (JSON 출력)")
    sp.add_argument("query", help="채널 URL, @핸들, 또는 채널명")

    sp = sub.add_parser("breakout-refs", help="떡상 영상 토픽으로 추가 레퍼런스 검색 (저장 없이 JSON 반환)")
    sp.add_argument("--video-id", required=True, dest="video_id")
    sp.add_argument("--days", type=int, default=30)
    sp.add_argument("--max-results", type=int, default=15, dest="max_results")

    args = p.parse_args()
    {
        "collect": cmd_collect,
        "filter": cmd_filter,
        "analyze": cmd_analyze,
        "run": cmd_run,
        "serve": cmd_serve,
        "promote": cmd_promote,
        "archive": cmd_archive,
        "unarchive": cmd_unarchive,
        "unpromote": cmd_unpromote,
        "cand-search": cmd_cand_search,
        "cand-save-refs": cmd_cand_save_refs,
        "cand-start": cmd_cand_start,
        "ytmatch": cmd_ytmatch,
        "ytrun": cmd_ytrun,
        "newsradar": cmd_newsradar,
        "resolve-channel": cmd_resolve_channel,
        "breakout-refs": cmd_breakout_refs,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
