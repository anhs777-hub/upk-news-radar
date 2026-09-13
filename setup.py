"""뉴스 레이더 셋업 스크립트.

Python 3.9+ 환경에서 실행: python setup.py

기능: 가상환경 생성 → 의존성 설치 → .env 부트스트랩

수동 시범 운영 모드. 자동 스케줄러 등록은 하지 않는다.
LLM 호출은 로컬 `claude` CLI(Claude Code 구독)를 subprocess로 사용하므로
ANTHROPIC_API_KEY 불필요.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQS = ROOT / "requirements.txt"
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


# ============================================================
#  유틸리티
# ============================================================

def run(cmd, **kwargs):
    print(f"  > {cmd}")
    subprocess.check_call(cmd, shell=True, **kwargs)


def get_python() -> str:
    if sys.platform == "win32":
        return str(VENV / "Scripts" / "python.exe")
    return str(VENV / "bin" / "python")


# ============================================================
#  1. venv
# ============================================================

def setup_venv():
    if VENV.exists():
        print("[1/3] 가상환경 이미 존재 - 건너뜀")
        return
    print("[1/3] 가상환경 생성")
    run(f'"{sys.executable}" -m venv "{VENV}"')


# ============================================================
#  2. 의존성
# ============================================================

def install_deps():
    print("[2/3] 의존성 설치")
    py = get_python()
    run(f'"{py}" -m pip install -U pip')
    if REQS.exists():
        run(f'"{py}" -m pip install -r "{REQS}"')
    else:
        run(f'"{py}" -m pip install feedparser python-dateutil')


# ============================================================
#  3. .env 부트스트랩 (선택)
# ============================================================

def bootstrap_env():
    print("[3/3] .env 파일 확인 (선택 — API 키 불필요)")
    if ENV_PATH.exists():
        print("  .env 이미 존재 - 건너뜀")
        return
    if ENV_EXAMPLE.exists():
        shutil.copyfile(ENV_EXAMPLE, ENV_PATH)
        print("  .env.example → .env 복사됨")
    else:
        ENV_PATH.write_text(
            "# 뉴스 레이더 환경변수 (현재는 사용 안 함. claude CLI 구독 인증 사용)\n",
            encoding="utf-8",
        )
        print("  .env 신규 생성 (빈 파일)")


# ============================================================
#  메인
# ============================================================

def main():
    print("=" * 50)
    print("  뉴스 레이더 셋업 (수동 시범 운영, 공유 X)")
    print("=" * 50)
    print()

    setup_venv()
    install_deps()
    bootstrap_env()

    print()
    print("=" * 50)
    print("셋업 완료.")
    print("수동 운영 모드입니다. 분석은 `python radar.py run`으로 직접 실행하세요.")
    print("대시보드: http://localhost:8080/news-radar/index.html (씬업대 서버 공유)")
    print("=" * 50)


if __name__ == "__main__":
    main()
