@echo off
rem Rated Gallery: one-time install. Needs internet for this step only.
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" not found. Install Python 3.11 from https://www.python.org/downloads/ and run this again.
  exit /b 1
)
py -3.11 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 is not installed. Install Python 3.11 ^(64-bit^) from python.org and run this again.
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Creating the Python environment...
  py -3.11 -m venv .venv || exit /b 1
)
.venv\Scripts\python -m pip install --upgrade pip --disable-pip-version-check -q || exit /b 1
echo Installing packages...
.venv\Scripts\python -m pip install -r requirements.txt --disable-pip-version-check || exit /b 1
echo Downloading models (about 1.6 GB) and exporting TOPIQ-NR (temporary PyTorch, deleted after)...
.venv\Scripts\python -m ratedgallery.setup_models || exit /b 1
echo.
echo Install is complete. Start the app with start.bat
endlocal
