@echo off
rem ============================================================================
rem  Registers the "Bookflea watcher" scheduled task. Double-click to run.
rem
rem  ASCII ONLY -- DO NOT PUT NON-ASCII CHARACTERS IN THIS FILE.
rem  All logic and all Ukrainian text live in install_task.py. See the header
rem  of that file for why: a .bat with Cyrillic comments plus "chcp" silently
rem  corrupts its own parsing.
rem ============================================================================

cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% install_task.py
pause