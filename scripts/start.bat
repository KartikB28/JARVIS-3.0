@echo off
REM CHAPPIE start script (Windows)
REM Activates the virtual environment and launches the FastAPI server.

setlocal
cd /d "%~dp0\.."

if not exist ".venv" (
  echo No .venv found. Run scripts\setup.bat first.
  exit /b 1
)

call .venv\Scripts\activate.bat

cd backend
echo ==^> CHAPPIE running at http://localhost:8000
echo     Press Ctrl+C to stop.
python -m uvicorn main:app --host 0.0.0.0 --port 8000
endlocal
