@echo off
REM Daily ingest wrapper for Windows Task Scheduler.
REM Schedule daily ~07:00 (Brreg refreshes around 05:00 CET).

cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m brreg_leads ingest >> data\ingest.log 2>&1
