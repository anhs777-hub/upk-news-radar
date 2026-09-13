"""news-radar 미니 백엔드 (stdlib only).

포트 8091. 씬업대(8080, 정적 서빙)와 분리된 액션 트리거용 서버.
- /api/health, /api/status
- /api/run (radar.py run 백그라운드 실행)
- /api/promote, /api/archive
- /api/daily/<date>, /api/dates
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG_DIR = ROOT / "config"
DEFAULT_CHANNEL = "science"


def _list_channels() -> list[dict]:
    out: list[dict] = []
    if CONFIG_DIR.exists():
        for f in sorted(CONFIG_DIR.glob("*.json")):
            cid = f.stem
            try:
                cfg = json.loads(f.read_text(encoding="utf-8"))
                meta = cfg.get("channel", {}) or {}
            except Exception:
                meta = {}
            out.append({
                "id": cid,
                "name": meta.get("name") or cid,
                "subtitle": meta.get("subtitle") or "",
            })
    return out


def _valid_channel(ch: str) -> bool:
    if not ch or "/" in ch or "\\" in ch or ".." in ch:
        return False
    return (CONFIG_DIR / f"{ch}.json").exists()


def _channel_from(query: dict) -> str:
    ch = (query.get("channel", [DEFAULT_CHANNEL])[0] or DEFAULT_CHANNEL).strip()
    return ch if _valid_channel(ch) else DEFAULT_CHANNEL


def _ch_paths(channel: str) -> dict:
    base = DATA / channel
    return {
        "data": base,
        "daily": base / "daily",
        "yt_daily": base / "youtube" / "daily",
        "radar_state": base / "_radarstate.json",
    }

LOG_MAX = 200

# --- 채널별 실행 상태 ---
_channels_lock = threading.Lock()
_channel_runs: dict[str, dict] = {}  # channel -> {state, log, proc}


def _get_channel_run(ch: str) -> dict:
    """채널별 실행 슬롯 반환 (없으면 생성)."""
    if ch not in _channel_runs:
        _channel_runs[ch] = {
            "state": {
                "running": False,
                "started_at": None,
                "ended_at": None,
                "stage": "idle",
                "exit_code": None,
                "duration_sec": None,
                "cmd": None,
                "dry_run": False,
            },
            "log": deque(maxlen=LOG_MAX),
            "proc": None,
        }
    return _channel_runs[ch]

# --- API 키 메모리 저장소 (디스크 기록 X) ---
# _API_KEYS = {"youtube": <첫키>, "anthropic": ..., "youtube_keys": [{"profile","key"}, ...]}
_API_KEYS: dict = {}
_KEYS_LOCK = threading.Lock()

# 키별 오늘 상태 — {키뒤8자: {"exhausted": bool, "units": int}}.
# radar.py 실행이 429를 만나면 data/_newsradar/_keystate.json 에 기록하고, 서버가 읽어 표시.
# 정확한 유닛 수는 구글이 실시간 제공하지 않으므로 '소진 여부' 중심 + 대략 추정 유닛.
_KEYSTATE_PATH = ROOT / "data" / "_newsradar" / "_keystate.json"


def _usage_maybe_reset() -> None:
    """UTC 날짜가 바뀌면 소진 상태 초기화 (쿼터는 태평양 자정 리셋이지만 근사)."""
    global _KEY_USAGE, _KEY_USAGE_DATE
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # 파일에서 로드 (radar.py가 기록)
    state = {}
    try:
        if _KEYSTATE_PATH.exists():
            state = json.loads(_KEYSTATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if state.get("date") != today:
        _KEY_USAGE = {}
        _KEY_USAGE_DATE = today
    else:
        _KEY_USAGE = state.get("usage", {}) or {}
        _KEY_USAGE_DATE = today


_KEY_USAGE: dict = {}
_KEY_USAGE_DATE: str = ""


def _mask_key(k: str) -> str:
    if not k or len(k) < 8:
        return "***"
    return f"{k[:4]}...{k[-4:]}"


def _youtube_key_list() -> list[str]:
    """저장된 YouTube 키 목록(순서 유지). youtube_keys 우선, 없으면 단일 youtube."""
    with _KEYS_LOCK:
        arr = _API_KEYS.get("youtube_keys") or []
        keys = [x.get("key", "").strip() for x in arr if x.get("key", "").strip()]
        if not keys and _API_KEYS.get("youtube"):
            keys = [_API_KEYS["youtube"]]
    # 중복 제거
    seen: set = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def _keys_env() -> dict:
    env = os.environ.copy()
    # 자식 Python 프로세스의 stdout 인코딩을 utf-8로 강제 (Windows cp949 깨짐 방지)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    yt_keys = _youtube_key_list()
    if yt_keys:
        env["YOUTUBE_API_KEYS"] = ",".join(yt_keys)  # youtube.py가 폴백에 사용
        env["YOUTUBE_API_KEY"] = yt_keys[0]           # 하위호환
    with _KEYS_LOCK:
        if _API_KEYS.get("anthropic"):
            env["ANTHROPIC_API_KEY"] = _API_KEYS["anthropic"]
    return env


def _test_youtube_key(key: str) -> dict:
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        "?part=id&chart=mostPopular&maxResults=1&regionCode=KR&key=" + key
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "news-radar/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "items" in data:
                return {"valid": True, "quota_used": None, "quota_max": 10000, "note": "트렌딩 1건 회수 OK"}
            return {"valid": False, "error": "unexpected_response"}
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            msg = body.get("error", {}).get("message", str(e))
        except Exception:
            msg = f"HTTP {e.code}"
        return {"valid": False, "error": msg}
    except Exception as e:
        return {"valid": False, "error": str(e)}


def _python_exe() -> str:
    if os.name == "nt":
        return str(ROOT / ".venv" / "Scripts" / "python.exe")
    return str(ROOT / ".venv" / "bin" / "python")


# Windows에서 subprocess가 새 콘솔 창을 띄우지 않도록 하는 공용 kwargs.
# server.py가 콘솔 없이(pythonw/백그라운드) 돌더라도, 자식이 콘솔 앱(schtasks, python)이면
# OS가 순간적으로 콘솔 창을 띄운다("분류 누를 때마다 창 깜빡" 증상). CREATE_NO_WINDOW로 차단.
def _no_window_kwargs() -> dict:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def _persist_state(channel: str = DEFAULT_CHANNEL) -> None:
    try:
        ch_dir = DATA / channel
        ch_dir.mkdir(parents=True, exist_ok=True)
        run = _get_channel_run(channel)
        (ch_dir / "_state.json").write_text(
            json.dumps({**run["state"], "log_tail": list(run["log"])[-50:]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _detect_stage(line: str) -> str | None:
    low = line.lower()
    for key in ("collect", "filter", "score", "analyze", "copy", "render", "promote", "archive", "done"):
        if key in low:
            return key
    return None


def _reader_thread(proc: subprocess.Popen, channel: str) -> None:
    run = _get_channel_run(channel)
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            run["log"].append(line)
            stage = _detect_stage(line)
            if stage:
                with _channels_lock:
                    run["state"]["stage"] = stage
        proc.wait()
    finally:
        with _channels_lock:
            st = run["state"]
            st["running"] = False
            st["ended_at"] = datetime.utcnow().isoformat() + "Z"
            st["exit_code"] = proc.returncode
            if st.get("started_at"):
                try:
                    t0 = datetime.fromisoformat(st["started_at"].rstrip("Z"))
                    st["duration_sec"] = (datetime.utcnow() - t0).total_seconds()
                except Exception:
                    pass
            try:
                ch_dir = DATA / channel
                ch_dir.mkdir(parents=True, exist_ok=True)
                (ch_dir / "_lastrun.json").write_text(
                    json.dumps(
                        {
                            "stage": st["stage"],
                            "exit_code": st["exit_code"],
                            "duration_sec": st["duration_sec"],
                            "started_at": st["started_at"],
                            "ended_at": st["ended_at"],
                            "dry_run": st["dry_run"],
                            "log_tail": list(run["log"]),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception:
                pass
            _persist_state(channel)
        run["proc"] = None


def start_run(dry_run: bool, subcmd: str = "run", channel: str = DEFAULT_CHANNEL,
              with_channel: bool = True, extra_args: list | None = None) -> tuple[bool, str]:
    """radar.py 서브커맨드를 백그라운드 실행.

    with_channel=False면 --channel 인자를 빼고 부른다(newsradar 등 공용 트랙용).
    extra_args로 추가 인자(예: --category media)를 붙일 수 있다.
    싱글톤 락은 채널 단위라, 공용 트랙은 채널 슬롯 하나(DEFAULT_CHANNEL)를 점유한다.
    """
    with _channels_lock:
        run = _get_channel_run(channel)
        if run["state"]["running"]:
            return False, "already_running"
        cmd = [_python_exe(), "radar.py", subcmd]
        if with_channel:
            cmd += ["--channel", channel]
        if extra_args:
            cmd += list(extra_args)
        if dry_run:
            cmd.append("--dry-run")
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=_keys_env(),
                **_no_window_kwargs(),
            )
        except FileNotFoundError as e:
            return False, f"python_not_found: {e}"
        run["proc"] = proc
        run["log"].clear()
        run["state"].update(
            {
                "running": True,
                "started_at": datetime.utcnow().isoformat() + "Z",
                "ended_at": None,
                "stage": "starting",
                "exit_code": None,
                "duration_sec": None,
                "cmd": " ".join(cmd),
                "dry_run": dry_run,
            }
        )
        _persist_state(channel)
    threading.Thread(target=_reader_thread, args=(proc, channel), daemon=True).start()
    return True, "started"


# ---------------------------------------------------------------------------
# 떡상 감시기 자동 실행 — Windows 작업 스케줄러(schtasks) 토글
# ON: 매일 정해진 시각에 radar.py newsradar 실행 작업 등록
# OFF: 작업 삭제. 상태는 /Query로 조회.
# ---------------------------------------------------------------------------
SCHED_TASK_NAME = "NewsRadarBreakout"

# --- 떡상 감시기 다분류 config ---
_BREAKOUT_CONFIG_PATH = ROOT / "config" / "_breakout.json"
_BREAKOUT_LOCK = threading.Lock()


def _breakout_cfg() -> dict:
    try:
        return json.loads(_BREAKOUT_CONFIG_PATH.read_text(encoding="utf-8")) if _BREAKOUT_CONFIG_PATH.exists() else {}
    except Exception:
        return {}


def _save_breakout_cfg(cfg: dict) -> None:
    _BREAKOUT_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid_seg(s: str) -> bool:
    """경로 세그먼트 안전성 (분류 id·날짜 등)."""
    return bool(s) and "/" not in s and "\\" not in s and ".." not in s


def _new_cat_id(cats: list) -> str:
    """새 분류 id 발급. 쓰이지 않는 catN을 찾는다.

    id는 data/_newsradar/<id>/ 폴더명과 API 경로 세그먼트로만 쓰이는 내부값이라
    사람이 읽을 필요가 없다. 삭제 후 재추가로 폴더가 남아 있을 수 있으므로
    config뿐 아니라 데이터 폴더까지 비어 있는 번호를 고른다(옛 데이터 섞임 방지).
    """
    used = {(c.get("id") or "") for c in cats}
    base = ROOT / "data" / "_newsradar"
    n = 1
    while True:
        cid = f"cat{n}"
        if cid not in used and not (base / cid).exists():
            return cid
        n += 1


def _sched_query() -> dict:
    """등록된 작업 조회. {enabled, time?} 반환.

    XML 출력(/XML)을 파싱해 로케일 무관하게 시각을 읽는다.
    StartBoundary는 ISO 형식(YYYY-MM-DDTHH:MM:SS)이라 12/24시간제·언어 영향 없음.
    """
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", SCHED_TASK_NAME, "/XML"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            **_no_window_kwargs(),
        )
    except Exception as e:
        return {"enabled": False, "error": str(e)[:120]}
    if r.returncode != 0:
        return {"enabled": False}  # 작업 없음
    import re
    m = re.search(r"<StartBoundary>\s*\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})", r.stdout)
    return {"enabled": True, "time": m.group(1) if m else None}


def _sched_set(enabled: bool, hour: int = 9) -> dict:
    """enabled=True면 매일 hour시에 작업 등록(덮어쓰기), False면 삭제."""
    if not enabled:
        try:
            r = subprocess.run(
                ["schtasks", "/Delete", "/TN", SCHED_TASK_NAME, "/F"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                **_no_window_kwargs(),
            )
            return {"ok": r.returncode == 0, "enabled": False, "msg": (r.stdout or r.stderr)[-200:]}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}
    h = max(0, min(23, int(hour)))
    py = _python_exe()
    # cwd 보장 위해 cmd /c 로 감싸 ROOT로 이동 후 실행
    cmd_line = f'cmd /c "cd /d {ROOT} && \"{py}\" radar.py newsradar"'
    # XML로 등록 — StartWhenAvailable(놓친 작업 따라잡기)을 직접 명시.
    # 절전 위주 사용에선 메인 보정(떡상 탭 자동수집)이 담당하고 이건 보조.
    from xml.sax.saxutils import escape as _xesc
    today = datetime.utcnow().strftime("%Y-%m-%d")
    xml = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        '  <Triggers>\n'
        '    <CalendarTrigger>\n'
        f'      <StartBoundary>{today}T{h:02d}:00:00</StartBoundary>\n'
        '      <Enabled>true</Enabled>\n'
        '      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n'
        '    </CalendarTrigger>\n'
        '  </Triggers>\n'
        '  <Settings>\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>\n'
        '  </Settings>\n'
        '  <Actions>\n'
        f'    <Exec><Command>cmd</Command><Arguments>/c "cd /d {_xesc(str(ROOT))} &amp;&amp; \"{_xesc(py)}\" radar.py newsradar"</Arguments></Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    )
    import tempfile, os as _os
    xml_path = None
    try:
        # schtasks /Create /XML 은 파일 경로를 받는다. UTF-16으로 저장.
        fd, xml_path = tempfile.mkstemp(suffix=".xml")
        with _os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", SCHED_TASK_NAME, "/XML", xml_path, "/F"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            **_no_window_kwargs(),
        )
        ok = r.returncode == 0
        return {"ok": ok, "enabled": ok,
                "time": f"{h:02d}:00", "msg": (r.stdout or r.stderr)[-200:]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
    finally:
        if xml_path:
            try:
                _os.remove(xml_path)
            except OSError:
                pass


def _run_radar_sync(cmd_args: list[str], timeout: int = 120) -> dict:
    """radar.py 서브커맨드를 동기 실행하는 공용 실행기. cmd_args는 radar.py 뒤 인자 그대로."""
    cmd = [_python_exe(), "radar.py", *cmd_args]
    try:
        r = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_keys_env(),
            **_no_window_kwargs(),
        )
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": r.stdout[-4000:],
            "stderr": r.stderr[-2000:],
            "cmd": " ".join(cmd),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "cmd": " ".join(cmd)}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"python_not_found: {e}", "cmd": " ".join(cmd)}


def run_sync(args: list[str], timeout: int = 120, channel: str = DEFAULT_CHANNEL) -> dict:
    """promote/archive 같은 짧은 명령 동기 실행 (--channel 자동 삽입)."""
    sub = args[0] if args else ""
    rest = args[1:] if len(args) > 1 else []
    return _run_radar_sync([sub, "--channel", channel, *rest], timeout=timeout)


# --- HTTP 핸들러 ---
class Handler(BaseHTTPRequestHandler):
    server_version = "NewsRadarMini/1.0"

    # --- helpers ---
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("[server] %s - %s\n" % (self.address_string(), format % args))

    # --- verbs ---
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _serve_static(self, path: str) -> None:
        # 루트 → news-radar.html
        if path in ("", "/"):
            path = "/news-radar.html"
        # 경로 정규화 + 디렉토리 탈출 방지
        rel = path.lstrip("/").replace("\\", "/")
        if ".." in rel.split("/"):
            return self._json(403, {"error": "forbidden"})
        target = (ROOT / rel).resolve()
        try:
            target.relative_to(ROOT.resolve())
        except ValueError:
            return self._json(403, {"error": "forbidden"})
        if not target.exists() or not target.is_file():
            return self._json(404, {"error": "not_found", "path": path})
        ext = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".htm": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".ico": "image/x-icon",
            ".woff": "font/woff",
            ".woff2": "font/woff2",
            ".ttf": "font/ttf",
            ".txt": "text/plain; charset=utf-8",
            ".map": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        try:
            data = target.read_bytes()
        except Exception as e:
            return self._json(500, {"error": f"read_failed: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        ch = _channel_from(q)
        cp = _ch_paths(ch)
        if not path.startswith("/api/"):
            return self._serve_static(path)
        if path == "/api/channels":
            return self._json(200, {"channels": _list_channels(), "default": DEFAULT_CHANNEL})
        if path == "/api/keys":
            with _KEYS_LOCK:
                out = {}
                for prov in ("youtube", "anthropic"):
                    k = _API_KEYS.get(prov)
                    out[prov] = {"set": bool(k), "masked": _mask_key(k) if k else None}
                out["youtube_keys"] = [
                    {"profile": x.get("profile", ""), "masked": _mask_key(x.get("key", ""))}
                    for x in (_API_KEYS.get("youtube_keys") or [])
                ]
            return self._json(200, out)
        if path == "/api/keys/usage":
            # 키별 오늘 사용량(추정 유닛)/소진 여부. 프론트는 키 뒤 8자로 매칭.
            _usage_maybe_reset()
            with _KEYS_LOCK:
                usage = dict(_KEY_USAGE)
            return self._json(200, usage)
        if path == "/api/health":
            return self._json(200, {"ok": True, "version": "1.0"})
        if path == "/api/status":
            with _channels_lock:
                run = _get_channel_run(ch)
                st = run["state"]
                snap = {
                    "running": st["running"],
                    "started_at": st["started_at"],
                    "ended_at": st["ended_at"],
                    "stage": st["stage"],
                    "exit_code": st["exit_code"],
                    "duration_sec": st["duration_sec"],
                    "dry_run": st["dry_run"],
                    "cmd": st.get("cmd"),
                    "log_lines": len(run["log"]),
                    "log_tail": list(run["log"])[-30:],
                }
            # 증분 모드 정보: last_run_at, next_window_hours
            last_run_at = None
            next_window_hours = None
            hours_since_last = None
            window_note = None
            try:
                rsp = cp["radar_state"]
                if rsp.exists():
                    rs = json.loads(rsp.read_text(encoding="utf-8")) or {}
                    last_run_at = rs.get("last_run_at")
            except Exception:
                pass
            try:
                from datetime import datetime as _dt, timezone as _tz
                # src.filter.compute_window 재사용
                sys.path.insert(0, str(ROOT))
                from src import filter as _fil  # type: ignore
                now = _dt.now(_tz.utc)
                cutoff, note = _fil.compute_window(now, {"last_run_at": last_run_at} if last_run_at else {})
                next_window_hours = round((now - cutoff).total_seconds() / 3600, 1)
                window_note = note
                if last_run_at:
                    try:
                        lrd = _dt.fromisoformat(last_run_at)
                        if lrd.tzinfo is None:
                            lrd = lrd.replace(tzinfo=_tz.utc)
                        hours_since_last = round((now - lrd).total_seconds() / 3600, 1)
                    except Exception:
                        pass
            except Exception:
                pass
            snap["last_run_at"] = last_run_at
            snap["hours_since_last"] = hours_since_last
            snap["next_window_hours"] = next_window_hours
            snap["window_note"] = window_note
            snap["channel"] = ch
            return self._json(200, snap)
        if path == "/api/dates":
            dates: list[str] = []
            d = cp["daily"]
            if d.exists():
                for f in d.glob("*.json"):
                    if f.stem == "index":
                        continue
                    dates.append(f.stem)
            dates.sort(reverse=True)
            return self._json(200, {"dates": dates, "channel": ch})
        if path == "/api/youtube/dates":
            dates: list[str] = []
            d = cp["yt_daily"]
            if d.exists():
                for f in d.glob("*.json"):
                    if f.stem == "index":
                        continue
                    dates.append(f.stem)
            dates.sort(reverse=True)
            return self._json(200, {"dates": dates, "channel": ch})
        if path.startswith("/api/youtube/"):
            date = path[len("/api/youtube/"):].strip("/")
            if not date or "/" in date or "\\" in date or ".." in date:
                return self._json(400, {"error": "bad_date"})
            f = cp["yt_daily"] / f"{date}.json"
            if not f.exists():
                return self._json(404, {"error": "not_found", "date": date})
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                return self._json(500, {"error": f"read_failed: {e}"})
            return self._json(200, data)
        if path.startswith("/api/daily/"):
            date = path[len("/api/daily/"):].strip("/")
            if not date or "/" in date or "\\" in date or ".." in date:
                return self._json(400, {"error": "bad_date"})
            f = cp["daily"] / f"{date}.json"
            if not f.exists():
                return self._json(404, {"error": "not_found", "date": date})
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                return self._json(500, {"error": f"read_failed: {e}"})
            return self._json(200, data)
        # --- 떡상 감시기 (다분류) ---
        if path == "/api/newsradar/schedule":
            return self._json(200, _sched_query())
        if path == "/api/newsradar/categories":
            cfg = _breakout_cfg()
            cats = [{"id": c.get("id"), "name": c.get("name"),
                     "channel_count": len(c.get("channels", []) or []),
                     "auto_collect": c.get("auto_collect", True),
                     "breakout": {**(cfg.get("defaults", {}) or {}), **(c.get("breakout", {}) or {})}}
                    for c in cfg.get("categories", []) or []]
            return self._json(200, {"categories": cats})
        # /api/newsradar/<cat>/channels  |  /latest  |  /dates  |  /date/<d>
        if path.startswith("/api/newsradar/") and path != "/api/newsradar/schedule":
            rest = path[len("/api/newsradar/"):].strip("/")
            parts = rest.split("/")
            cat_id = parts[0]
            sub = parts[1] if len(parts) > 1 else ""
            if not _valid_seg(cat_id):
                return self._json(400, {"error": "bad_category"})
            cfg = _breakout_cfg()
            cat = next((c for c in cfg.get("categories", []) or [] if c.get("id") == cat_id), None)
            if sub == "channels":
                if not cat:
                    return self._json(404, {"error": "no_category"})
                return self._json(200, {"channels": cat.get("channels", []) or [],
                                        "name": cat.get("name")})
            ddir = ROOT / "data" / "_newsradar" / cat_id / "daily"
            if sub == "dates":
                dates = sorted((f.stem for f in ddir.glob("*.json") if f.stem != "index"),
                               reverse=True) if ddir.exists() else []
                return self._json(200, {"dates": dates})
            if sub == "latest" or sub == "date":
                if sub == "latest":
                    files = sorted((f for f in ddir.glob("*.json") if f.stem != "index"),
                                   key=lambda f: f.stem, reverse=True) if ddir.exists() else []
                    if not files:
                        return self._json(404, {"error": "no_data", "category": cat_id})
                    f = files[0]
                else:
                    date = parts[2] if len(parts) > 2 else ""
                    if not _valid_seg(date):
                        return self._json(400, {"error": "bad_date"})
                    f = ddir / f"{date}.json"
                    if not f.exists():
                        return self._json(404, {"error": "not_found", "date": date})
                try:
                    return self._json(200, json.loads(f.read_text(encoding="utf-8")))
                except Exception as e:
                    return self._json(500, {"error": f"read_failed: {e}"})
        return self._json(404, {"error": "not_found", "path": path})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/keys":
            qs = parse_qs(urlparse(self.path).query)
            prov = (qs.get("provider", [""])[0] or "").strip()
            if prov not in ("youtube", "anthropic"):
                return self._json(400, {"error": "bad_provider"})
            with _KEYS_LOCK:
                _API_KEYS.pop(prov, None)
            return self._json(200, {"removed": prov})
        return self._json(404, {"error": "not_found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        body = self._read_json()
        # 채널: body.channel 우선, 없으면 쿼리, 없으면 default
        q = parse_qs(u.query)
        ch_raw = (body.get("channel") or q.get("channel", [DEFAULT_CHANNEL])[0] or DEFAULT_CHANNEL)
        ch = ch_raw if _valid_channel(ch_raw) else DEFAULT_CHANNEL
        if path == "/api/keys":
            saved, removed = [], []
            with _KEYS_LOCK:
                # 다중 YouTube 키 배열
                if "youtube_keys" in body:
                    arr = body.get("youtube_keys") or []
                    clean = [{"profile": str(x.get("profile", "")).strip(),
                              "key": str(x.get("key", "")).strip()}
                             for x in arr if str(x.get("key", "")).strip()]
                    if clean:
                        _API_KEYS["youtube_keys"] = clean
                        saved.append(f"youtube_keys({len(clean)})")
                    else:
                        _API_KEYS.pop("youtube_keys", None)
                        removed.append("youtube_keys")
                for prov in ("youtube", "anthropic"):
                    if prov not in body:
                        continue
                    val = body.get(prov)
                    if val is None or (isinstance(val, str) and not val.strip()):
                        if _API_KEYS.pop(prov, None) is not None:
                            removed.append(prov)
                    elif isinstance(val, str):
                        _API_KEYS[prov] = val.strip()
                        saved.append(prov)
            return self._json(200, {"saved": saved, "removed": removed})
        if path == "/api/keys/test":
            prov = str(body.get("provider") or "").strip()
            if prov == "anthropic":
                return self._json(200, {"valid": True, "note": "claude CLI 사용 중 (API 키 불필요)"})
            if prov == "youtube":
                if body.get("all"):
                    # 저장된 모든 YouTube 키 개별 테스트
                    results = []
                    for k in _youtube_key_list():
                        r = _test_youtube_key(k)
                        results.append({"tail": k[-8:], "valid": bool(r.get("valid")),
                                        "error": r.get("error"), "note": r.get("note")})
                    return self._json(200, {"results": results})
                with _KEYS_LOCK:
                    key = _API_KEYS.get("youtube") or (
                        (_API_KEYS.get("youtube_keys") or [{}])[0].get("key"))
                if not key:
                    return self._json(200, {"valid": False, "error": "키가 저장되지 않음"})
                return self._json(200, _test_youtube_key(key))
            return self._json(400, {"error": "bad_provider"})
        if path == "/api/run":
            dry = bool(body.get("dry_run", False))
            ok, msg = start_run(dry, subcmd="run", channel=ch)
            if not ok:
                status = 409 if msg == "already_running" else 500
                return self._json(status, {"error": msg})
            return self._json(200, {"started": True, "dry_run": dry, "channel": ch})
        if path == "/api/ytrun":
            dry = bool(body.get("dry_run", False))
            ok, msg = start_run(dry, subcmd="ytrun", channel=ch)
            if not ok:
                status = 409 if msg == "already_running" else 500
                return self._json(status, {"error": msg})
            return self._json(200, {"started": True, "dry_run": dry, "cmd": "ytrun", "channel": ch})
        if path == "/api/newsradar":
            # 떡상 감시 실행. category 있으면 그 분류만(지금 감지), 없으면 전체(자동과 동일).
            dry = bool(body.get("dry_run", False))
            cat = str(body.get("category") or "").strip()
            extra = ["--category", cat] if cat and _valid_seg(cat) else None
            ok, msg = start_run(dry, subcmd="newsradar", channel=DEFAULT_CHANNEL,
                                with_channel=False, extra_args=extra)
            if not ok:
                status = 409 if msg == "already_running" else 500
                return self._json(status, {"error": msg})
            return self._json(200, {"started": True, "dry_run": dry, "cmd": "newsradar", "category": cat})
        if path == "/api/newsradar/schedule":
            enabled = bool(body.get("enabled", False))
            hour = int(body.get("hour", 9))
            return self._json(200, _sched_set(enabled, hour))
        if path == "/api/newsradar/category":
            # 분류 추가/삭제.  {action:"add"|"delete", id, name}
            # add는 id를 받지 않는다 — 이름만 받고 서버가 발급한다(아래 _new_cat_id).
            action = str(body.get("action") or "").strip()
            cid = str(body.get("id") or "").strip()
            if action != "add" and not _valid_seg(cid):
                return self._json(400, {"error": "bad_id"})
            with _BREAKOUT_LOCK:
                cfg = _breakout_cfg()
                cats = cfg.setdefault("categories", [])
                if action == "add":
                    name = str(body.get("name") or "").strip()
                    if not name:
                        return self._json(400, {"error": "missing_name"})
                    if any((c.get("name") or "").strip() == name for c in cats):
                        return self._json(200, {"ok": False, "error": "같은 이름의 분류가 이미 있습니다"})
                    # id는 폴더명·URL 경로로만 쓰이는 내부 식별자다. 사용자가 정할 이유가 없어
                    # 서버가 발급한다. 한글 이름을 로마자로 옮기면 규칙이 애매하고 충돌도 나므로
                    # 순번(cat1, cat2…)으로 간다. 화면에는 항상 name만 보인다.
                    cid = _new_cat_id(cats)
                    # track_threshold는 명시하지 않고 defaults(1만)를 따른다 —
                    # 양산형의 단시간 1만 급성장을 새 분류에서도 기본으로 잡기 위함.
                    # 새 분류는 자동 수집 꺼짐으로 시작한다. 채널이 아직 없어
                    # 매일 헛돌 뿐이고, 운영 준비가 되면 ⚙에서 켠다.
                    cats.append({"id": cid, "name": name,
                                 "breakout": {"hot_views": 100000},
                                 "auto_collect": False,
                                 "channels": []})
                    _save_breakout_cfg(cfg)
                    return self._json(200, {"ok": True, "id": cid, "name": name})
                if action == "delete":
                    cfg["categories"] = [c for c in cats if c.get("id") != cid]
                    _save_breakout_cfg(cfg)
                    return self._json(200, {"ok": True, "deleted": cid})
                if action == "toggle_auto":
                    cat = next((c for c in cats if c.get("id") == cid), None)
                    if not cat:
                        return self._json(404, {"error": "no_category"})
                    cat["auto_collect"] = bool(body.get("enabled", True))
                    _save_breakout_cfg(cfg)
                    return self._json(200, {"ok": True, "auto_collect": cat["auto_collect"]})
                return self._json(400, {"error": "bad_action"})
        if path == "/api/newsradar/channel":
            # 채널 등록/제거.  {action:"add"|"remove", category, url|channel_id}
            action = str(body.get("action") or "").strip()
            cid = str(body.get("category") or "").strip()
            if not _valid_seg(cid):
                return self._json(400, {"error": "bad_category"})
            if action == "add":
                q = str(body.get("url") or "").strip()
                if not q:
                    return self._json(400, {"error": "missing_url"})
                # URL/핸들/검색어 → channel_id 변환은 subprocess(radar resolve-channel)로 위임.
                # (서버 프로세스 안에서 youtube API 직접 호출 시 스코프/환경 문제 회피)
                res = _run_radar_sync(["resolve-channel", q], timeout=40)
                resolved = None
                if res.get("ok") and res.get("stdout"):
                    # stdout 마지막 JSON 라인 파싱
                    for line in reversed(res["stdout"].strip().splitlines()):
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                resolved = json.loads(line)
                            except Exception:
                                resolved = None
                            break
                if not resolved or not resolved.get("ok") or not resolved.get("channel_id"):
                    err = (resolved or {}).get("error") or "채널을 찾을 수 없음"
                    return self._json(200, {"ok": False, "error": err})
                with _BREAKOUT_LOCK:
                    cfg = _breakout_cfg()
                    cat = next((c for c in cfg.get("categories", []) if c.get("id") == cid), None)
                    if not cat:
                        return self._json(404, {"error": "no_category"})
                    chs = cat.setdefault("channels", [])
                    if any(c.get("channel_id") == resolved["channel_id"] for c in chs):
                        return self._json(200, {"ok": False, "error": "이미 등록된 채널",
                                                "name": resolved.get("name")})
                    chs.append({"name": resolved.get("name", ""), "channel_id": resolved["channel_id"]})
                    _save_breakout_cfg(cfg)
                    return self._json(200, {"ok": True, "name": resolved.get("name"),
                                            "channel_id": resolved["channel_id"]})
            if action == "remove":
                target = str(body.get("channel_id") or "").strip()
                with _BREAKOUT_LOCK:
                    cfg = _breakout_cfg()
                    cat = next((c for c in cfg.get("categories", []) if c.get("id") == cid), None)
                    if not cat:
                        return self._json(404, {"error": "no_category"})
                    cat["channels"] = [c for c in cat.get("channels", []) if c.get("channel_id") != target]
                    _save_breakout_cfg(cfg)
                    return self._json(200, {"ok": True, "removed": target})
            return self._json(400, {"error": "bad_action"})
        if path == "/api/promote":
            item_id = str(body.get("id") or "").strip()
            if not item_id:
                return self._json(400, {"error": "missing_id"})
            return self._json(200, run_sync(["promote", "--id", item_id], channel=ch))
        if path == "/api/archive":
            item_id = str(body.get("id") or "").strip()
            if not item_id:
                return self._json(400, {"error": "missing_id"})
            return self._json(200, run_sync(["archive", "--id", item_id], channel=ch))
        if path == "/api/unarchive":
            item_id = str(body.get("id") or "").strip()
            if not item_id:
                return self._json(400, {"error": "missing_id"})
            return self._json(200, run_sync(["unarchive", "--id", item_id], channel=ch))
        if path == "/api/unpromote":
            item_id = str(body.get("id") or "").strip()
            if not item_id:
                return self._json(400, {"error": "missing_id"})
            return self._json(200, run_sync(["unpromote", "--id", item_id], channel=ch))
        if path == "/api/cand-search":
            item_id = str(body.get("id") or "").strip()
            if not item_id:
                return self._json(400, {"error": "missing_id"})
            days = str(body.get("days", 30))
            max_results = str(body.get("max_results", 15))
            return self._json(200, run_sync(
                ["cand-search", "--id", item_id, "--days", days, "--max-results", max_results],
                channel=ch, timeout=60
            ))
        if path == "/api/cand-save-refs":
            item_id = str(body.get("id") or "").strip()
            video_ids = body.get("video_ids", [])
            if not item_id:
                return self._json(400, {"error": "missing_id"})
            import json as _json
            return self._json(200, run_sync(
                ["cand-save-refs", "--id", item_id, "--video-ids", _json.dumps(video_ids)],
                channel=ch
            ))
        if path == "/api/cand-start":
            item_id = str(body.get("id") or "").strip()
            if not item_id:
                return self._json(400, {"error": "missing_id"})
            return self._json(200, run_sync(
                ["cand-start", "--id", item_id],
                channel=ch, timeout=300
            ))
        return self._json(404, {"error": "not_found", "path": path})


def _kill_child_processes() -> None:
    """서버 종료 시 실행 중인 radar.py 자식 프로세스를 모두 정리."""
    with _channels_lock:
        for ch, run in _channel_runs.items():
            proc = run.get("proc")
            if proc and proc.poll() is None:
                sys.stderr.write(f"[news-radar] killing child: radar.py ({ch}, pid={proc.pid})\n")
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


def run_server(host: str, port: int) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"[news-radar] mini backend on http://{host}:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stderr.write("[news-radar] shutting down\n")
        _kill_child_processes()
        server.server_close()


if __name__ == "__main__":
    import argparse
    # pythonw.exe(콘솔 없음)로 실행하면 sys.stdout/stderr가 None이라, 곳곳의
    # sys.stderr.write(...)와 subprocess의 stdout=sys.stderr 등이 AttributeError로
    # 터진다(요청 처리 중이면 그 요청이 죽음). None이면 무해한 싱크로 대체해 방어한다.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8091)
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args()
    run_server(args.host, args.port)
