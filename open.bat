@echo off
REM 이 파일은 UTF-8로 저장돼 있다. 한글이 깨지지 않게 코드페이지를 먼저 바꾼다.
REM (chcp보다 위에서는 ASCII만 쓸 것 — 위에서 한글을 쓰면 cmd가 명령어로 오독한다.)
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"
title News Radar

REM ============================================================
REM  Run News Radar - just double-click this file.
REM    1) create .venv + install packages (first run only)
REM    2) start backend on 8091
REM    3) open browser
REM  Heavy package (embedding) installs in background after launch.
REM ============================================================

set PORT=8091
set PY=.venv\Scripts\python.exe
set PYW=.venv\Scripts\pythonw.exe

REM ---- 1) first run: create venv -----------------------------
if not exist "%PY%" (
    echo.
    echo  [처음 실행] 준비 중입니다. 1~2분 걸립니다. 창을 닫지 마세요.
    echo.

    set "BOOT="
    where python >nul 2>&1 && set "BOOT=python"
    if not defined BOOT (
        where py >nul 2>&1 && set "BOOT=py -3"
    )
    if not defined BOOT (
        echo  [!] 파이썬이 설치돼 있지 않습니다.
        echo      https://www.python.org/downloads/ 에서 설치하세요.
        echo      설치 화면에서 "Add Python to PATH" 를 반드시 체크해야 합니다.
        echo.
        pause
        exit /b 1
    )

    echo  - 가상환경 생성 중...
    !BOOT! -m venv .venv
    if not exist "%PY%" (
        echo  [!] 가상환경 생성 실패. 파이썬 설치 상태를 확인하세요.
        pause
        exit /b 1
    )

    echo  - 필수 패키지 설치 중...
    "%PY%" -m pip install -q --upgrade pip
    "%PY%" -m pip install -q feedparser python-dateutil
    if errorlevel 1 (
        echo  [!] 패키지 설치 실패. 인터넷 연결을 확인하세요.
        pause
        exit /b 1
    )

    REM 임베딩 패키지는 수 GB라 기다리지 않는다. 없어도 나머지 기능은 동작한다.
    echo  - 부가 기능^(유사 뉴스 매칭^) 은 백그라운드로 설치합니다.
    start "" /B "%PY%" -m pip install -q sentence-transformers numpy

    echo.
    echo  준비 완료. 브라우저를 엽니다.
    echo.
)

REM ---- 2) start backend if not listening ----------------------
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    REM pythonw = no console window. server.py has stderr=None guard.
    if exist "%PYW%" (
        start "" /B "%PYW%" server.py --port %PORT%
    ) else (
        start "" /B "%PY%" server.py --port %PORT%
    )

    REM wait until it actually listens (max 20s)
    for /L %%i in (1,1,20) do (
        timeout /t 1 /nobreak >nul
        netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>&1
        if not errorlevel 1 goto :ready
    )
    echo  [!] 서버가 응답하지 않습니다. 아래 명령으로 오류를 확인하세요.
    echo      "%PY%" server.py --port %PORT%
    pause
    exit /b 1
)

:ready
start "" "http://localhost:%PORT%/news-radar.html"
exit /b 0
