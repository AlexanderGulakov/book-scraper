@echo off
rem Увімкнути щоденне стеження за Букфлі (завдання почне спрацьовувати за розкладом).
chcp 65001 >nul
schtasks /Change /TN "Bookflea watcher" /ENABLE
if errorlevel 1 (
  echo.
  echo Не вдалось. Схоже, завдання ще не створене — див. README, розділ "Букфлі вдома".
) else (
  echo.
  echo Готово: Букфлі перевірятиметься кожні 30 хвилин, поки комп'ютер увімкнений.
)
pause
