@echo off
rem One-step start: creates .venv, installs dependencies, creates .env, then runs the app.
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & exit /b 1)
)
.venv\Scripts\python -m pip install -q -r requirements.txt || exit /b 1
if not exist .env (
  copy .env.example .env >nul
  echo Created .env from .env.example
)
echo.
echo   App:   http://127.0.0.1:8000   (demo login: demo@example.com / demo1234)
echo   Claude AI: add your Anthropic API key in Settings ^> AI
echo.
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
