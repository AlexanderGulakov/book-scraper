@echo off
rem Enable the Bookflea watcher task. ASCII only -- see run-bookflea.bat header.
schtasks /Change /TN "Bookflea watcher" /ENABLE
if errorlevel 1 echo Task not found. Run bookflea-install.bat first.
pause