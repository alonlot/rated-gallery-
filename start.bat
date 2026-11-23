@echo off
rem Rated Gallery: start the app. Runs fully offline.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run install.bat first.
  exit /b 1
)
set HF_HUB_OFFLINE=1
set TRANSFORMERS_OFFLINE=1
set HF_DATASETS_OFFLINE=1
.venv\Scripts\python -m ratedgallery %*
