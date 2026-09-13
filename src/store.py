"""파일 IO 유틸. jsonl/json 읽고 쓰기."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Any

# 증분 모드용 상태 파일 (server.py의 _state.json과 분리)
STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "_radarstate.json"


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_jsonl(path: str | Path, items: Iterable[dict]) -> int:
    p = Path(path)
    ensure_dir(p.parent)
    n = 0
    with p.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
            n += 1
    return n


def append_jsonl(path: str | Path, items: Iterable[dict]) -> int:
    p = Path(path)
    ensure_dir(p.parent)
    n = 0
    with p.open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
            n += 1
    return n


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 증분 모드 상태 (마지막 실행 시각 등)
# ---------------------------------------------------------------------------
def read_state(path: str | Path | None = None) -> dict:
    p = Path(path) if path else STATE_PATH
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_state(state: dict, path: str | Path | None = None) -> None:
    p = Path(path) if path else STATE_PATH
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def update_last_run(at: datetime | None = None, path: str | Path | None = None) -> None:
    s = read_state(path)
    s["last_run_at"] = (at or datetime.now(timezone.utc)).isoformat()
    write_state(s, path)
