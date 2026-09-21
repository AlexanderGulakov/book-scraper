@echo off
rem Stop and disable the Bookflea watcher task, aborting a run in progress.
rem ASCII only -- see run-bookflea.bat header.
schtasks /End    /TN "Bookflea watcher" >nul 2>nul
schtasks /Change /TN "Bookflea watcher" /DISABLE
if errorlevel 1 echo Task not found. Run bookflea-install.bat first.
pause