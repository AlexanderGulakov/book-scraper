@echo off
rem ============================================================================
rem  Букфлі: один прогін. Цей файл запускає Планувальник Windows.
rem
rem  Навіщо окремий .bat, а не команда прямо в завданні: тут видно, що саме
rem  виконується, і це можна правити без перестворення завдання.
rem
rem  Чому Букфлі крутиться вдома, а не в GitHub Actions: сайт вибирає каталог
rem  за IP клієнта, і з американської адреси віддає польський ринок.
rem ============================================================================

rem UTF-8 у консолі: без цього кирилиця в логах перетворюється на кашу
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

cd /d "%~dp0"

rem py.exe — стандартний лаунчер Python на Windows; якщо його немає, пробуємо python
where py >nul 2>nul && (set PY=py -3) || (set PY=python)

if not exist logs mkdir logs

rem --only bookflea  — лише книгарня (решту пошуків робить GitHub Actions)
rem --state          — ОКРЕМИЙ файл стану, щоб не воювати з тим, що пушить хмара
%PY% main.py --only bookflea --state state.local.json >> "logs\bookflea.log" 2>&1

rem Лишаємо в логу розділювач із часом, щоб прогони не зливались в одну стіну
echo --- %date% %time% exit=%errorlevel% >> "logs\bookflea.log"
