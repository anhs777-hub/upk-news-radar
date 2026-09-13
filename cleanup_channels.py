# -*- coding: utf-8 -*-
"""등록 채널 점검·정리 (뉴스레이더).

YouTube API로 등록 채널의 생존을 확인해 아래를 구분한다:
  dead    — API가 못 찾음(삭제·정지·비공개). 매 감지마다 404를 내며 헛돈다.
  empty   — 살아있으나 영상 0개. 수집할 게 없다.
  renamed — 채널명이 바뀜. 이름만 갱신하면 되는 경우가 대부분.

주제를 갈아탄 채널(야담→쇼핑 등)은 API로 판별할 수 없다. renamed 목록을
사람이 보고 판단해 --remove 로 개별 지정한다.

기본은 조회만 한다(아무것도 바꾸지 않음).
  python cleanup_channels.py                    점검만
  python cleanup_channels.py --fix-names        바뀐 이름만 갱신
  python cleanup_channels.py --remove-dead      응답 없는 채널 제거
  python cleanup_channels.py --remove UCxxx UCyyy   지정 채널 제거

비용: 채널 50개당 1유닛(조회). 248개면 5유닛.
"""
from __future__ import annotations
import argparse, io, json, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

CFG = ROOT / "config" / "_breakout.json"


def fetch_alive(ids: list[str]) -> dict:
    """channels.list로 생존 채널 정보 조회. 50개당 1유닛."""
    from src import llm, youtube as yt
    llm.load_env(ROOT / ".env")
    alive: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        try:
            data = yt._get("channels", {"part": "snippet,statistics", "id": ",".join(chunk)})
        except Exception as e:
            print(f"[조회 실패] {e}", file=sys.stderr)
            continue
        for it in data.get("items", []):
            st = it.get("statistics", {}) or {}
            alive[it["id"]] = {
                "title": (it.get("snippet", {}) or {}).get("title", ""),
                "videos": int(st.get("videoCount", 0) or 0),
                "subs": int(st.get("subscriberCount", 0) or 0),
            }
    return alive


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-names", action="store_true", help="바뀐 채널명을 최신으로 갱신")
    ap.add_argument("--remove-dead", action="store_true", help="응답 없는 채널(삭제·정지) 제거")
    ap.add_argument("--remove-empty", action="store_true", help="영상 0개 채널도 함께 제거")
    ap.add_argument("--remove", nargs="*", default=[], metavar="CHANNEL_ID",
                    help="지정한 channel_id 제거 (주제 갈아탄 채널 등)")
    a = ap.parse_args()

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    rows = [(c, ch) for c in cfg["categories"] for ch in (c.get("channels") or [])]
    ids = sorted({ch["channel_id"] for _, ch in rows})
    print(f"등록 채널 {len(rows)}개 (고유 {len(ids)}개) — 조회 {(len(ids)+49)//50}유닛\n")

    alive = fetch_alive(ids)
    dead    = [(c, ch) for c, ch in rows if ch["channel_id"] not in alive]
    empty   = [(c, ch) for c, ch in rows if ch["channel_id"] in alive and alive[ch["channel_id"]]["videos"] == 0]
    renamed = [(c, ch, alive[ch["channel_id"]]["title"]) for c, ch in rows
               if ch["channel_id"] in alive
               and alive[ch["channel_id"]]["title"].strip() != ch["name"].strip()]

    print(f"살아있음 {len(alive)} · 응답없음 {len(dead)} · 영상0개 {len(empty)} · 이름변경 {len(renamed)}\n")
    if dead:
        print("=== 응답 없음 (삭제·정지·비공개) ===")
        for c, ch in dead:
            print(f"  [{c['name']}] {ch['name']}  {ch['channel_id']}")
    if empty:
        print("\n=== 영상 0개 ===")
        for c, ch in empty:
            print(f"  [{c['name']}] {ch['name']}  {ch['channel_id']}")
    if renamed:
        print("\n=== 이름 변경 (주제까지 바뀐 건 --remove 로 개별 지정) ===")
        for c, ch, new in renamed:
            print(f"  [{c['name']}] {ch['name']}  →  {new}   {ch['channel_id']}")

    todo = a.fix_names or a.remove_dead or a.remove_empty or a.remove
    if not todo:
        print("\n점검만 했습니다. 반영하려면 --fix-names / --remove-dead / --remove <id…>")
        return 0

    shutil.copy2(CFG, CFG.with_suffix(".json.bak"))
    drop = set(a.remove)
    if a.remove_dead:
        drop |= {ch["channel_id"] for _, ch in dead}
    if a.remove_empty:
        drop |= {ch["channel_id"] for _, ch in empty}

    removed, renamed_n = [], 0
    for c in cfg["categories"]:
        keep = []
        for ch in (c.get("channels") or []):
            if ch["channel_id"] in drop:
                removed.append((c["name"], ch["name"], ch["channel_id"]))
                continue
            if a.fix_names and ch["channel_id"] in alive:
                new = alive[ch["channel_id"]]["title"].strip()
                if new and new != ch["name"].strip():
                    ch["name"] = new
                    renamed_n += 1
            keep.append(ch)
        c["channels"] = keep

    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== 반영 ===")
    if removed:
        print(f"  제거 {len(removed)}개")
        for cat, name, cid in removed:
            print(f"    - [{cat}] {name}  {cid}")
    if renamed_n:
        print(f"  이름 갱신 {renamed_n}개")
    print(f"\n  백업: {CFG.name}.bak")
    for c in cfg["categories"]:
        print(f"    {c['name']:6} {len(c.get('channels') or []):4}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
