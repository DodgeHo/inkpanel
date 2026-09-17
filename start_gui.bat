@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem  H9 Dashboard Console - launcher (no console window)
rem
rem  Probe order:
rem     1. project .venv
rem     2. known-good absolute interpreter (Python312-32)   <-- preferred
rem     3. pyw -3
rem     4. pythonw found on PATH
rem
rem  The check is ALWAYS done with the SAME interpreter that gets
rem  launched: it must import BOTH tkinter (GUI) and PIL (rendering).
rem  Checking one interpreter and launching another is how this script
rem  used to fail silently (PATH pythonw != py -3).
rem
rem  NOTE: keep this file pure ASCII. cmd.exe parses .bat using the
rem  local codepage; UTF-8 Chinese text corrupts the whole line.
rem ---------------------------------------------------------------

rem --- the interpreter this project is known to work with ---
set "KNOWNW=%LOCALAPPDATA%\Programs\Python\Python312-32\pythonw.exe"
set "KNOWNC=%LOCALAPPDATA%\Programs\Python\Python312-32\python.exe"

if exist "%~dp0.venv\Scripts\pythonw.exe" goto usevenv
if exist "%KNOWNW%" goto useknown

where pyw >nul 2>nul
if %errorlevel%==0 goto usepyw

where pythonw >nul 2>nul
if %errorlevel%==0 goto usepythonw

goto nopython

rem ---------------- 1. project virtualenv ----------------
:usevenv
"%~dp0.venv\Scripts\python.exe" -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: .venv\Scripts\pythonw.exe
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0topsir_gui.py"
exit /b 0

rem ---------------- 2. known-good absolute interpreter ----------------
:useknown
"%KNOWNC%" -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: %KNOWNW%
start "" "%KNOWNW%" "%~dp0topsir_gui.py"
exit /b 0

rem ---------------- 3. py launcher ----------------
:usepyw
py -3 -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: pyw -3
start "" pyw -3 "%~dp0topsir_gui.py"
exit /b 0

rem ---------------- 4. pythonw from PATH ----------------
:usepythonw
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    set "PYW=%%i"
    goto havepyw
)
goto nopython

:havepyw
set "PYC=%PYW:pythonw.exe=python.exe%"
if not exist "%PYC%" goto notk
"%PYC%" -c "import tkinter, PIL" >nul 2>nul
if errorlevel 1 goto notk
echo Using: %PYW%
start "" "%PYW%" "%~dp0topsir_gui.py"
exit /b 0

rem ---------------- errors ----------------
:notk
echo.
echo [ERROR] The interpreter found is missing a required module.
echo         Checked with the SAME interpreter that would be launched:
echo            tkinter  (GUI toolkit)
echo            PIL      (Pillow - image rendering)
echo.
echo Fix Pillow:   python -m pip install -r requirements.txt
echo Fix tkinter:  re-run the Python installer -^> Modify -^>
echo               tick "tcl/tk and IDLE".
echo.
echo Known-good interpreter expected at:
echo     %KNOWNC%
echo.
echo To see the full error, run:  start_gui_console.bat
echo.
pause
exit /b 1

:nopython
echo.
echo [ERROR] No usable Python found (need Python 3.8+).
echo.
echo Install Python from python.org and tick:
echo    - "Add python.exe to PATH"
echo    - "tcl/tk and IDLE"
echo.
echo Or create a virtualenv in this folder:
echo     python -m venv .venv
echo     .venv\Scripts\pip install -r requirements.txt
echo.
pause
exit /b 1
