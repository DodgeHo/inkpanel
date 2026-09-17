@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem  Run the dashboard HTTP server with no GUI (headless / autostart).
rem
rem  Probe order:  .venv  ->  known-good Python312-32  ->  py -3  ->  python
rem  Validated with the SAME interpreter that gets launched (needs PIL).
rem
rem  NOTE: keep this file pure ASCII - cmd.exe parses .bat as the
rem  local codepage, so UTF-8 Chinese would corrupt the whole line.
rem ---------------------------------------------------------------

set "KNOWNC=%LOCALAPPDATA%\Programs\Python\Python312-32\python.exe"
set "PORT=8765"

if exist "%~dp0.venv\Scripts\python.exe" goto usevenv
if exist "%KNOWNC%" goto useknown

where py >nul 2>nul
if %errorlevel%==0 goto usepy

where python >nul 2>nul
if %errorlevel%==0 goto usepython

goto nopython

:usevenv
"%~dp0.venv\Scripts\python.exe" -c "import PIL" >nul 2>nul
if errorlevel 1 goto nopil
echo Using: .venv\Scripts\python.exe
"%~dp0.venv\Scripts\python.exe" "%~dp0dashboard\server.py" --port %PORT%
exit /b 0

:useknown
"%KNOWNC%" -c "import PIL" >nul 2>nul
if errorlevel 1 goto nopil
echo Using: %KNOWNC%
"%KNOWNC%" "%~dp0dashboard\server.py" --port %PORT%
exit /b 0

:usepy
py -3 -c "import PIL" >nul 2>nul
if errorlevel 1 goto nopil
echo Using: py -3
py -3 "%~dp0dashboard\server.py" --port %PORT%
exit /b 0

:usepython
python -c "import PIL" >nul 2>nul
if errorlevel 1 goto nopil
echo Using: python
python "%~dp0dashboard\server.py" --port %PORT%
exit /b 0

:nopil
echo.
echo [ERROR] Pillow (PIL) is missing in the interpreter that would be used,
echo         so the dashboard image cannot be rendered.
echo.
echo Fix:  python -m pip install -r requirements.txt
echo Known-good interpreter expected at:
echo    %KNOWNC%
echo.
pause
exit /b 1

:nopython
echo.
echo [ERROR] No usable Python found.
echo Install Python 3.8+, or create a virtualenv here:
echo     python -m venv .venv
echo     .venv\Scripts\pip install -r requirements.txt
echo.
pause
exit /b 1
