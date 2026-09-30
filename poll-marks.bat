@echo off
rem ============================================================================
rem  Telegram button poller. Launched by Windows Task Scheduler every minute.
rem
rem  ASCII ONLY -- DO NOT PUT NON-ASCII CHARACTERS IN THIS FILE.
rem  See run-bookflea.bat for the three days this rule once cost.
rem
rem  Why a separate task. A button under a notification keeps spinning until
rem  something answers the callback, and the full run happens once every 15
rem  minutes. By then Telegram may already refuse to answer that callback id,
rem  so the spinner never clears - especially on the phone.
rem
rem  --poll-marks does getUpdates, applies the marks, saves, exits. No OLX
rem  request at all, about a second of work, so it is cheap to run often.
rem  It does NOT scrape and does NOT send anything.
rem ============================================================================

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0"

where py >nul 2>nul && (set PY=py -3) || (set PY=python)

if not exist logs mkdir logs

%PY% main.py --poll-marks --storage mongo >> "logs\marks.log" 2>&1
