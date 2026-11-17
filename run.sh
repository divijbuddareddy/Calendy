#!/usr/bin/env sh
# One-step start: creates .venv, installs dependencies, creates .env, then runs the app.
set -e
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
if [ ! -d .venv ]; then echo "Creating virtual environment..."; $PY -m venv .venv; fi
if [ -x .venv/bin/python ]; then VPY=.venv/bin/python; else VPY=.venv/Scripts/python; fi
$VPY -m pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example"; }
echo
echo "  App:   http://127.0.0.1:8000   (demo login: demo@example.com / demo1234)"
echo "  Claude AI: add your Anthropic API key in Settings > AI"
echo
exec $VPY -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
