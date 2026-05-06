@echo off
REM Run CHAPPIE as a desktop app (development mode — no packaging).
REM Starts the FastAPI server in-process and opens a native window.

setlocal
cd /d "%~dp0\.."

if not exist ".venv" (
  echo No .venv found. Run scripts\setup.bat first.
  exit /b 1
)

call .venv\Scripts\activate.bat

REM pywebview is the desktop window. If missing, the launcher falls back
REM to the default browser, but you really want it for the full experience.
python -m pip show pywebview >nul 2>nul
if errorlevel 1 (
  echo Installing pywebview...
  python -m pip install pywebview
)

python desktop_app.py
endlocal
