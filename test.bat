@echo off
rem Run the automated tests on synthetic data (downloads public-domain test portraits once).
cd /d "%~dp0"
.venv\Scripts\python -m pytest -q tests
