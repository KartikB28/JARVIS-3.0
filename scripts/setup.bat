@echo off
REM CHAPPIE setup script (Windows)
REM Creates a virtual environment in .venv and installs Python dependencies.

setlocal
cd /d "%~dp0\.."

echo ==^> Checking for Python...
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH.
  echo Install it from https://www.python.org/downloads/ ^(check "Add Python to PATH"^).
  exit /b 1
)
python --version

echo ==^> Creating virtual environment in .venv ...
if not exist ".venv" (
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo ==^> Upgrading pip...
python -m pip install --upgrade pip

echo ==^> Installing CHAPPIE dependencies...
python -m pip install -r backend\requirements.txt

if not exist "data" mkdir data
if not exist "logs" mkdir logs

echo.
echo All done. Start CHAPPIE with:  scripts\start.bat
endlocal
