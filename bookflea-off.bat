@echo off
rem Зупинити стеження за Букфлі: вимикає завдання і перериває прогін, якщо він саме йде.
chcp 65001 >nul
schtasks /End    /TN "Bookflea watcher" >nul 2>nul
schtasks /Change /TN "Bookflea watcher" /DISABLE
if errorlevel 1 (
  echo.
  echo Не вдалось. Схоже, завдання ще не створене — див. README, розділ "Букфлі вдома".
) else (
  echo.
  echo Зупинено. Стан збережено, тож після увімкнення бот продовжить з того ж місця.
)
pause
