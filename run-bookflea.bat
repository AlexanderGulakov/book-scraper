@echo off
rem ============================================================================
rem  Bookflea watcher: one run. Launched by Windows Task Scheduler.
rem
rem  ASCII ONLY -- DO NOT PUT NON-ASCII CHARACTERS IN THIS FILE.
rem
rem  This file used to carry Ukrainian comments plus "chcp 65001". That
rem  combination silently destroys the script: cmd.exe re-reads a .bat from a
rem  byte offset after every command, so switching the code page midway
rem  through a file that contains multi-byte characters shifts those offsets.
rem  The parser then splits comment lines in half and tries to execute the
rem  tails, e.g.  'peretvoryuyetsya' is not recognized as a command.
rem  It happened to work on one machine and broke on another - see README,
rem  section "Bookflea vdoma".
rem
rem  chcp is gone for good: output goes to a FILE, not to the console, and
rem  PYTHONUTF8/PYTHONIOENCODING already make Python write UTF-8 there.
rem  Explanations live in README.md, not here.
rem ============================================================================

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0"

rem py.exe is the standard Python launcher on Windows; fall back to python.exe
where py >nul 2>nul && (set PY=py -3) || (set PY=python)

if not exist logs mkdir logs

rem --only bookflea : OLX searches are handled by GitHub Actions
rem --storage mongo : state lives in MongoDB Atlas now, shared with the cloud.
rem                   The old separate state.local.json is gone: with
rem                   document-level writes both writers touch different
rem                   documents, so there is nothing left to overwrite.
rem                   "mongo" and not "auto" on purpose - if MONGODB_URI is
rem                   missing from .env the run must fail loudly instead of
rem                   quietly starting a second, local state file.
%PY% main.py --only bookflea --storage mongo >> "logs\bookflea.log" 2>&1

echo --- %date% %time% exit=%errorlevel% >> "logs\bookflea.log"