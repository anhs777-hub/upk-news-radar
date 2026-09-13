#!/bin/bash
cd "$(dirname "$0")"
if ! lsof -i:8091 -sTCP:LISTEN -t >/dev/null 2>&1; then
    nohup .venv/bin/python server.py --port 8091 >/dev/null 2>&1 &
    sleep 2
fi
open http://localhost:8091/news-radar.html 2>/dev/null || xdg-open http://localhost:8091/news-radar.html 2>/dev/null
