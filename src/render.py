"""분석 결과 → daily/{date}.json + index.json 갱신."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone
from . import store


def build_daily(
    date: str,
    raw_count: int,
    filtered_count: int,
    enriched: list[dict],
    fronts: list[dict],
    threshold_candidate: float,
    threshold_watch: float,
) -> dict:
    front_by_id = {f["id"]: f for f in fronts}

    def _attach_front(it: dict) -> dict:
        f = front_by_id.get(it.get("front_id", ""), {})
        return {
            **it,
            "front_name": f.get("name", ""),
            "front_color": f.get("color", "#888"),
            "front_daily_axes": f.get("daily_axes", []),
        }

    enriched_full = [_attach_front(it) for it in enriched]
    enriched_full.sort(key=lambda x: x.get("total", 0), reverse=True)

    candidates = [it for it in enriched_full if it.get("total", 0) >= threshold_candidate]
    watch = [
        it for it in enriched_full
        if threshold_watch <= it.get("total", 0) < threshold_candidate
    ]

    return {
        "date": date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "raw_count": raw_count,
            "filtered_count": filtered_count,
            "candidate_count": len(candidates),
            "watch_count": len(watch),
        },
        "thresholds": {
            "candidate": threshold_candidate,
            "watch": threshold_watch,
        },
        "items": enriched_full,
    }


def save_daily(data_dir: str | Path, daily: dict) -> Path:
    data_dir = Path(data_dir)
    daily_dir = data_dir / "daily"
    store.ensure_dir(daily_dir)
    out_path = daily_dir / f"{daily['date']}.json"
    store.write_json(out_path, daily)
    update_index(data_dir)
    return out_path


def update_index(data_dir: str | Path) -> Path:
    """daily/*.json 목록을 index.json으로 갱신."""
    daily_dir = Path(data_dir) / "daily"
    store.ensure_dir(daily_dir)
    dates = sorted(
        [p.stem for p in daily_dir.glob("*.json") if p.name != "index.json"],
        reverse=True,
    )
    idx_path = daily_dir / "index.json"
    store.write_json(idx_path, {"dates": dates, "latest": dates[0] if dates else None})
    return idx_path
