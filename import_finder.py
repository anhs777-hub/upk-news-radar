# -*- coding: utf-8 -*-
"""유튜브파인더 백업(JSON) → 뉴스레이더 분류에 채널 등록.

파인더는 채널을 브라우저 localStorage에 두므로 파일로 직접 읽을 수 없다.
파인더 화면의 [📤 백업 파일로 내보내기]로 받은 JSON을 이 스크립트에 넘긴다.

형식 변환:
  파인더  [{id, name, channels:[{id, title, ...}]}]
  레이더  categories[].channels = [{name, channel_id}]

사용:
  python import_finder.py 백업.json --folder 야담 --category yadam
  python import_finder.py 백업.json --list          (폴더 목록만 보기)
  python import_finder.py 백업.json --folder 경제 --category economy --dry-run
"""
import argparse, json, sys, io, shutil
from pathlib import Path

# 윈도 콘솔 기본 cp949는 '—' 같은 문자를 못 찍어 죽는다. UTF-8로 고정.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "config" / "_breakout.json"


def load(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("backup", help="유튜브파인더 백업 JSON 경로")
    ap.add_argument("--folder", help="가져올 파인더 폴더 이름")
    ap.add_argument("--category", help="넣을 뉴스레이더 분류 id")
    ap.add_argument("--list", action="store_true", help="백업 안의 폴더 목록만 출력")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 결과만 표시")
    a = ap.parse_args()

    folders = load(a.backup)
    if not isinstance(folders, list):
        print("백업 형식이 아닙니다(최상위가 배열이어야 함)", file=sys.stderr)
        return 2

    if a.list or not (a.folder and a.category):
        print("=== 백업 안의 폴더 ===")
        for f in folders:
            print(f"  {f.get('name','(이름없음)')} — 채널 {len(f.get('channels') or [])}개")
        cfg = load(CFG)
        print("\n=== 뉴스레이더 분류 ===")
        for c in cfg.get("categories", []):
            print(f"  {c.get('id'):12} {c.get('name')} — 채널 {len(c.get('channels') or [])}개")
        if not (a.folder and a.category):
            print("\n--folder 와 --category 를 지정해 실행하세요.")
        return 0

    src = next((f for f in folders if f.get("name") == a.folder), None)
    if src is None:
        print(f"폴더 '{a.folder}' 없음. --list 로 확인하세요.", file=sys.stderr)
        return 2

    cfg = load(CFG)
    cat = next((c for c in cfg.get("categories", []) if c.get("id") == a.category), None)
    if cat is None:
        print(f"분류 '{a.category}' 없음. --list 로 확인하세요.", file=sys.stderr)
        return 2

    existing = cat.get("channels") or []
    have = {c.get("channel_id") for c in existing}
    added, dup = [], []
    for ch in src.get("channels") or []:
        cid = ch.get("id")
        if not cid:
            continue
        if cid in have:
            dup.append(ch.get("title") or cid)
            continue
        have.add(cid)
        added.append({"name": ch.get("title") or cid, "channel_id": cid})

    print(f"[{a.folder}] → [{a.category}]  기존 {len(existing)}개")
    print(f"  추가 {len(added)}개 / 중복 건너뜀 {len(dup)}개")
    for c in added:
        print(f"    + {c['name']}  ({c['channel_id']})")
    if dup:
        print(f"  (중복) {', '.join(dup[:8])}{' ...' if len(dup) > 8 else ''}")

    if a.dry_run:
        print("\n--dry-run: 저장하지 않았습니다.")
        return 0
    if not added:
        print("\n추가할 채널이 없어 파일을 건드리지 않았습니다.")
        return 0

    bak = CFG.with_suffix(".json.bak")
    shutil.copy2(CFG, bak)
    cat["channels"] = existing + added
    CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n저장 완료 — {CFG}  (백업: {bak.name})")
    print(f"  [{a.category}] 최종 {len(cat['channels'])}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
