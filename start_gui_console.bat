@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem  Same as start_gui.bat, but keeps the console window open so you
rem  can read tracebacks. Use this when the GUI will not start.
rem
rem  Probe order:  .venv  ->  known-good Python312-32  ->  py -3  ->  python
rem  Validated with the SAME interpreter that gets launched.
rem
rem  NOTE: keep this file pure ASCII.
rem ---------------------------------------------------------------

set "KNOWNC=%LOCALAPPDATA%\Programs\Python\Python312-32\python.exe"

echo === H9 Dashboard Console (console mode) ===
echo.

if exist "%~dp0.venv\Scripts\python.exe" goto usevenv
if exist "%KNOWNC%" goto useknown

where py >nul 2>nul
if %errorlevel%==0 goto usepy

where python >nul 2>nul
if %errorlevel%==0 goto usepython

echo [ERROR] No usable Python found (need Python 3.8+).
echo Install it from python.org, then run this again.
goto end

:usevenv
"%~dp0.venv\Scripts\python.exe" -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: .venv\Scripts\python.exe
echo.
"%~dp0.venv\Scripts\python.exe" "%~dp0topsir_gui.py"
goto end

:useknown
"%KNOWNC%" -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: %KNOWNC%
echo.
"%KNOWNC%" "%~dp0topsir_gui.py"
goto end

:usepy
py -3 -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: py -3
echo.
py -3 "%~dp0topsir_gui.py"
goto end

:usepython
python -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: python
echo.
python "%~dp0topsir_gui.py"
goto end

:notk
echo [ERROR] Required modules missing in the interpreter that would run:
echo            tkinter (GUI)  and/or  PIL (Pillow).
echo.
echo Fix:  python -m pip install -r requirements.txt
echo       (tkinter comes from the Python installer: Modify -^> "tcl/tk and IDLE")
echo.

:end
echo.
echo === process exited ===
pause
