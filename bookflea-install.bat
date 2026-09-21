@echo off
rem ============================================================================
rem  Створює завдання Планувальника «Bookflea watcher» — кожні 30 хвилин.
rem  Подвійний клік, і все. Перестворює завдання, якщо воно вже є.
rem
rem  Навіщо окремий файл, а не команда з README: у README шлях був написаний
rem  як C:\Users\<ви>\..., і його один раз скопіювали дослівно, разом із <ви>.
rem  Завдання створилось, показувало Ready, справно «спрацьовувало» кожні
rem  30 хвилин — і щоразу падало з 0x1, бо такого шляху немає. Тут шлях
rem  береться з %~dp0, тобто з розташування самого файлу: помилитись нічим.
rem ============================================================================

chcp 65001 >nul
setlocal

set "TASK=Bookflea watcher"
set "TARGET=%~dp0run-bookflea.bat"

if not exist "%TARGET%" (
  echo [X] Не знайшов "%TARGET%".
  echo     Запускайте цей файл із теки проєкту, поруч із run-bookflea.bat.
  goto :done
)

echo Завдання : %TASK%
echo Запускає : %TARGET%
echo Розклад  : кожні 30 хвилин
echo.

schtasks /Query /TN "%TASK%" >nul 2>nul
if not errorlevel 1 (
  echo Таке завдання вже є — перестворюю з правильним шляхом.
  schtasks /Delete /TN "%TASK%" /F >nul
)

schtasks /Create /TN "%TASK%" /SC MINUTE /MO 30 /TR "\"%TARGET%\"" /F
if errorlevel 1 (
  echo.
  echo [X] Не вдалось створити завдання.
  goto :done
)

echo.
echo [OK] Створено. Перевіряю, що воно справді запускається...
schtasks /Run /TN "%TASK%" >nul
rem Прогін Букфлі — це 11 запитів із паузами, тобто близько хвилини.
timeout /t 75 /nobreak >nul

for /f "tokens=2 delims=:" %%R in ('schtasks /Query /TN "%TASK%" /V /FO LIST ^| findstr /C:"Last Result"') do set "RES=%%R"
set "RES=%RES: =%"

echo.
if "%RES%"=="0" (
  echo [OK] Останній прогін завершився без помилок.
  echo      Далі — кожні 30 хвилин. Лог: "%~dp0logs\bookflea.log"
) else (
  echo [!] Останній прогін повернув код %RES%.
  echo     Найчастіші причини:
  echo       1 - немає .env або каналу Telegram. Перевірте: py main.py --test-notify
  echo       2 - не знайдено файл. Перевірте шлях у завданні.
  echo     Подробиці: "%~dp0logs\bookflea.log"
)

:done
echo.
pause
endlocal
