"""claude CLI subprocess 래퍼.

Anthropic API 대신 로컬에 설치된 `claude` CLI를 subprocess로 호출한다.
Claude Code 구독 인증을 그대로 재사용하므로 ANTHROPIC_API_KEY 불필요.

- classify_and_score: 배치 분류+점수 (batch_size=80 기본)
- generate_copy: 후보 1건 카피 생성
- _call_claude: 공용 헬퍼 (claude -p --output-format json)
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# .env 로딩 (python-dotenv 의존성 없이) — 더 이상 API 키 강제 안 함
# ---------------------------------------------------------------------------
def load_env(env_path: str | Path = ".env") -> None:
    p = Path(env_path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# claude CLI 호출 공용 헬퍼
# ---------------------------------------------------------------------------
def _find_claude() -> str:
    """Windows에서 npm 글로벌 .cmd 래퍼를 우선 탐색."""
    found = shutil.which("claude")
    if found:
        return found
    if sys.platform == "win32":
        for cand in [
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
            Path(os.environ.get("APPDATA", "")) / "npm" / "claude.ps1",
        ]:
            if cand.exists():
                return str(cand)
    return "claude"


def _no_window_kwargs() -> dict:
    """Windows에서 claude CLI(cmd 래퍼 포함)가 콘솔 창을 띄우지 않게 한다.
    감지 실행 중 콘솔 창이 깜빡이는 현상을 막는다."""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def _call_claude(prompt: str, system: str = "", timeout: int = 180, max_retries: int = 3) -> str:
    """claude -p 한 번 호출. result 필드(실제 모델 응답)를 문자열로 반환.

    실패 시 지수 백오프 재시도 (2s → 5s → 10s). 서버 자동 기동 직후 Claude Code
    세션이 워밍업되지 않은 상태에서 첫 호출이 stderr 없이 returncode 1로 끝나는
    현상을 흡수한다.
    """
    exe = _find_claude()
    cmd = [
        exe, "-p",
        "--tools", "",
        "--output-format", "json",
        "--model", "claude-haiku-4-5-20251001",
    ]
    if system:
        cmd += ["--system-prompt", system]
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat", ".ps1")):
        cmd = ["cmd", "/c"] + cmd

    backoffs = [2, 5, 10]
    last_err = ""
    for attempt in range(max_retries):
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **_no_window_kwargs(),
            )
        except FileNotFoundError:
            raise RuntimeError("claude CLI를 찾을 수 없습니다. Claude Code가 설치되어 있는지 확인하세요.")
        except subprocess.TimeoutExpired as e:
            last_err = f"timeout({timeout}s)"
            print(f"[llm] call attempt {attempt+1}/{max_retries} 타임아웃", file=sys.stderr)
        else:
            if proc.returncode == 0:
                try:
                    wrapper = json.loads(proc.stdout)
                    if isinstance(wrapper, dict) and "result" in wrapper:
                        return wrapper.get("result", "") or ""
                    return proc.stdout
                except json.JSONDecodeError:
                    return proc.stdout
            stderr_snip = (proc.stderr or "").strip()[:300]
            stdout_snip = (proc.stdout or "").strip()[:300]
            last_err = f"rc={proc.returncode} stderr={stderr_snip!r} stdout={stdout_snip!r}"
            print(f"[llm] call attempt {attempt+1}/{max_retries} 실패: {last_err}", file=sys.stderr)

        if attempt < max_retries - 1:
            import time
            time.sleep(backoffs[min(attempt, len(backoffs) - 1)])

    raise RuntimeError(f"claude CLI 실패 ({max_retries}회 재시도 후): {last_err}")


# ---------------------------------------------------------------------------
# 분류 + 점수 (배치)
# ---------------------------------------------------------------------------
CLASSIFIER_SYSTEM = """너는 한국 유튜브 과학 채널의 PD 어시스턴트다. 뉴스 항목을 9개 분야로 분류하고 5축으로 1~5점 매긴다.

[5축 점수 가이드]
- impact (충격도, weight 1.0): 숫자가 직관적으로 큰가 (조원, 배수, 배율). 5=세계급 충격, 1=뻔함
- daily (일상 연결, weight 1.2): 시청자 사물(폰/차/지갑/일자리/집)에 닿는가. 5=내일 당장 영향, 1=학계 내부
- novelty (새로움, weight 1.0): 최초 발표/돌파구/반전. 5=최초 공개, 1=재탕
- search (검색 잠재력, weight 0.8): 사람들이 찾을 키워드인가. 5=핫키워드, 1=무명
- visual (시각화, weight 0.8): 영상 만들기 쉬운가 (비교/실물/도식). 5=완벽한 그림, 1=추상적

[분야 정의]
{fronts_block}

[출력]
오직 JSON만 출력. 어떤 설명/마크다운 없이 다음 구조:
{{"results": [{{"id": "<항목id>", "front_id": "<분야id>", "scores": {{"impact": 1~5, "daily": 1~5, "novelty": 1~5, "search": 1~5, "visual": 1~5}}, "total": <가중합>, "one_line_summary": "<30자 이내 한국어>", "reason_brief": "<왜 이 분야/점수인지 한 줄>"}}]}}

총점 계산: impact*1.0 + daily*1.2 + novelty*1.0 + search*0.8 + visual*0.8
어떤 분야에도 안 맞으면 front_id="" 로 두고 total=0.
"""


def _fronts_block(fronts: list[dict]) -> str:
    lines = []
    for f in fronts:
        kws = ", ".join((f.get("keywords_ko", []) + f.get("keywords_en", []))[:10])
        lines.append(f"- {f['id']} ({f['name']}): {kws}")
    return "\n".join(lines)


def _compute_total(scores: dict, axes: list[dict]) -> float:
    total = 0.0
    for ax in axes:
        v = scores.get(ax["id"], 0)
        try:
            total += float(v) * float(ax.get("weight", 1.0))
        except Exception:
            pass
    return round(total, 2)


def classify_and_score(
    items: list[dict],
    fronts: list[dict],
    axes: list[dict],
    model: str | None = None,
    batch_size: int = 80,
) -> list[dict]:
    """items → 분류+점수가 추가된 리스트.

    배치(기본 80개씩) 묶어 한 번에 한 번 claude CLI 호출.
    model 파라미터는 무시됨 (CLI는 현재 세션 모델 사용).
    """
    if not items:
        return []
    system = CLASSIFIER_SYSTEM.format(fronts_block=_fronts_block(fronts))
    enriched: list[dict] = []

    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        compact = [
            {
                "id": it["id"],
                "title": it.get("title", "")[:200],
                "summary": (it.get("summary") or "")[:300],
                "source": it.get("source", ""),
                "lang": it.get("lang", "en"),
            }
            for it in batch
        ]
        user_msg = (
            "[CRITICAL OUTPUT CONTRACT]\n"
            "Respond with ONLY a single raw JSON object. NO markdown. NO tables. NO code fences. NO preamble. NO commentary.\n"
            "Your entire response must start with { and end with }.\n"
            "Schema: {\"results\": [{\"id\": \"...\", \"front_id\": \"...\", \"scores\": {\"impact\": 1-5, \"daily\": 1-5, \"novelty\": 1-5, \"search\": 1-5, \"visual\": 1-5}, \"one_line_summary\": \"30자 이내 한국어\", \"reason_brief\": \"한 줄\"}]}\n"
            "Use these front_id values exactly: " + ", ".join(f["id"] for f in fronts) + "\n"
            "If item doesn't fit any front, set front_id=\"\" and all scores to 1.\n"
            "Score guide: impact(충격), daily(일상연결), novelty(새로움), search(검색), visual(시각화) — each 1~5.\n\n"
            "ITEMS TO CLASSIFY:\n" + json.dumps(compact, ensure_ascii=False) + "\n\n"
            "Now output the JSON object only:"
        )

        try:
            text = _call_claude(user_msg, system=system, timeout=300)
            print(f"[llm] batch {start} 응답 길이={len(text or '')}, 앞 200자: {(text or '')[:200]!r}", file=sys.stderr)
            data = _safe_json(text)
            results = data.get("results", []) if isinstance(data, dict) else []
            print(f"[llm] batch {start} 파싱 결과 results={len(results)}건", file=sys.stderr)
        except Exception as e:
            print(f"[llm] classify 배치 {start} 실패: {e}", file=sys.stderr)
            results = []

        result_by_id = {r.get("id"): r for r in results if isinstance(r, dict)}
        for it in batch:
            r = result_by_id.get(it["id"], {})
            scores = r.get("scores", {}) if isinstance(r, dict) else {}
            total = _compute_total(scores, axes) if scores else 0.0
            enriched.append({
                **it,
                "front_id": r.get("front_id", "") if isinstance(r, dict) else "",
                "scores": scores,
                "total": total,
                "one_line_summary": r.get("one_line_summary", "") if isinstance(r, dict) else "",
                "reason_brief": r.get("reason_brief", "") if isinstance(r, dict) else "",
            })
    return enriched


def _safe_json(text: str) -> Any:
    """LLM 응답에서 JSON만 추출."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.startswith("```"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s = text.find("{")
        e = text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                return {}
    return {}


# ---------------------------------------------------------------------------
# 카피 생성 — pd-guide 톤 인라인
# ---------------------------------------------------------------------------
COPYWRITER_SYSTEM = """너는 한국 유튜브 과학 채널 PD다. 뉴스 한 건을 받아 영상감으로 다듬는 카피를 작성한다.

[톤 절대 규칙 — 채널 pd-guide 발췌]
1. Hook 절대 금지어: "지난", "이전", "1편/2편/N편", "전 영상", "저번", "앞서". Hook은 첫 시청자 기준.
2. Hook 첫 문단(첫 30초, 80~120자) 안에 2인칭("내", "네", "당신") 또는 일상 사물(폰/카톡/지갑/비밀번호/노트북/자동차) 1개 이상 필수.
3. 일상 진입은 Hook의 진입장벽을 깎는 도구다. "쉬움"과 "유아화"를 혼동하지 말 것. 본문 톤은 사실/수치/고유명사 중심의 "친한 형이 술자리에서 정확한 사실을 풀어주는" 느낌이 기준선.
4. 다음 편 떡밥 금지. 클로징은 그 영상 자체로 마감.
5. 학술 용어 단독 사용 금지. "결어긋남/패권/원천차단" 같은 한자어 추상어 X. "내 돈/털린다/끝/안전?" 같은 본능 단어 우선.

[CTR 기대도 8축 평가 — 네가 만든 hook/title 기준으로 1~5점]
- hook_strength: 첫 3초 멱살잡기. 사건/숫자/도발/장면이 즉시 등장하는가
- daily_entry: 30초 안에 시청자 사물(내 폰/내 차/내 카톡)이 등장하는가
- number_shock: 100억년→5분, 22조 같은 직관적 숫자 격차
- provocation: 안 보면 못 견디는 갭. 왜?/사실은/정반대
- search_volume: 사람들이 실제로 검색하는 단어인가
- familiarity: 들어봤지만 모르는 것. 너무 어렵지도 너무 뻔하지도 X
- thumbnail_match: 제목과 썸네일 텍스트가 같은 메시지를 주는가
- jargon_penalty: 학술/한자어 추상어 정도. 1=깨끗, 5=심각 (페널티)

[출력 — JSON만, 마크다운/설명 X]
{
  "hook_one_liner": "<시청자 도발 한 줄. 2인칭 또는 일상 사물 1개 이상 포함. 절대 금지어 X. 60자 이내>",
  "daily_angles": ["내 ~로 시작하는 일상 연결 한 줄", "내 ~", "내 ~"],
  "title_candidates": ["제목1 (쉬운 일상톤)", "제목2 (쉬운 일상톤)", "제목3 (\\"따옴표\\" + 키워드 강조형)"],
  "risk_note": "이 소재로 영상 만들 때 놓치면 안 될 함정 한 줄",
  "ctr_axes": {"hook_strength": 1, "daily_entry": 1, "number_shock": 1, "provocation": 1, "search_volume": 1, "familiarity": 1, "thumbnail_match": 1, "jargon_penalty": 1}
}

daily_angles는 반드시 3개, 각각 "내" 또는 "네"로 시작.
title_candidates는 반드시 3개. 1~2번은 일상 톤, 3번은 키워드 강조형.
ctr_axes는 위 8개 키 모두 1~5 정수로 채울 것.
"""


def compute_ctr_score(scores: dict, axes: list[dict]) -> dict:
    """CTR 8축 점수 → raw/normalized/tier."""
    if not axes:
        return {"raw": 0.0, "normalized": 0.0, "tier": "weak"}
    positive_max = 0.0
    negative_max = 0.0
    raw = 0.0
    for ax in axes:
        w = float(ax.get("weight", 0) or 0)
        if w > 0:
            positive_max += w * 5
        elif w < 0:
            negative_max += w * 5
        try:
            v = float(scores.get(ax["id"], 0) or 0)
        except Exception:
            v = 0.0
        raw += v * w
    span = positive_max - negative_max
    normalized = 0.0 if span <= 0 else round((raw - negative_max) / span * 100, 1)
    if normalized < 0:
        normalized = 0.0
    if normalized > 100:
        normalized = 100.0
    strong_th = 80.0
    ok_th = 65.0
    if normalized >= strong_th:
        tier = "strong"
    elif normalized >= ok_th:
        tier = "ok"
    else:
        tier = "weak"
    return {"raw": round(raw, 2), "normalized": normalized, "tier": tier}


def generate_copy(
    item: dict,
    front: dict,
    model: str | None = None,
    ctr_axes_def: list[dict] | None = None,
    ctr_thresholds: dict | None = None,
) -> dict:
    """threshold 통과 항목에 대해서만 호출. model 파라미터는 무시됨."""
    payload = {
        "title": item.get("title", ""),
        "summary": item.get("summary", ""),
        "source": item.get("source", ""),
        "front": front.get("name", ""),
        "front_daily_axes": front.get("daily_axes", []),
        "scores": item.get("scores", {}),
        "total": item.get("total", 0),
    }
    user_msg = (
        "[CRITICAL OUTPUT CONTRACT]\n"
        "Respond with ONLY a single raw JSON object. NO markdown. NO code fences. NO preamble. NO commentary.\n"
        "Your entire response must start with { and end with }.\n"
        "Schema:\n"
        '{"hook_one_liner": "...", "daily_angles": ["내 ...", "내 ...", "내 ..."], '
        '"title_candidates": ["...", "...", "..."], "risk_note": "...", '
        '"ctr_axes": {"hook_strength": 1-5, "daily_entry": 1-5, "number_shock": 1-5, '
        '"provocation": 1-5, "search_volume": 1-5, "familiarity": 1-5, '
        '"thumbnail_match": 1-5, "jargon_penalty": 1-5}}\n\n'
        "[톤 규칙]\n"
        "- Hook 첫 문단(60자 이내)에 2인칭(내/네/당신) 또는 일상사물(폰/카톡/지갑/차/비밀번호) 1개 이상 필수.\n"
        "- 금지어: 지난/이전/N편/저번/앞서.\n"
        "- 학술 한자어(결어긋남/패권/원천차단) 금지. 본능 단어(내 돈/털린다/끝/안전?) 우선.\n"
        "- daily_angles 3개 모두 '내' 또는 '네'로 시작.\n"
        "- title_candidates 3개. 1~2번 일상톤, 3번 키워드 강조형.\n"
        "- ctr_axes 8개 키 모두 1~5 정수 채울 것.\n\n"
        "[NEWS DATA]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\nNow output the JSON object only:"
    )
    try:
        text = _call_claude(user_msg, system=COPYWRITER_SYSTEM, timeout=240)
        data = _safe_json(text)
        if not isinstance(data, dict):
            return {}
        if ctr_axes_def:
            ctr_axes = data.get("ctr_axes", {}) or {}
            ctr_score = compute_ctr_score(ctr_axes, ctr_axes_def)
            if ctr_thresholds:
                strong_th = float(ctr_thresholds.get("strong", 80))
                ok_th = float(ctr_thresholds.get("ok", 65))
                n = ctr_score["normalized"]
                ctr_score["tier"] = (
                    "strong" if n >= strong_th else ("ok" if n >= ok_th else "weak")
                )
            data["ctr_score"] = ctr_score
        return data
    except Exception as e:
        print(f"[llm] copy 실패 ({item.get('id')}): {e}", file=sys.stderr)
        return {}


if __name__ == "__main__":
    pass
