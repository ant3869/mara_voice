@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "POWERSHELL_EXE=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "START_SCRIPT=%SCRIPT_DIR%start_mara.ps1"
set "GUI_FLAG=-Gui"

for %%A in (%*) do (
    if /I "%%~A"=="-Gui" set "GUI_FLAG="
    if /I "%%~A"=="-NoGui" set "GUI_FLAG="
)

if not exist "%START_SCRIPT%" (
    echo [mara] Could not find "%START_SCRIPT%"
    pause
    exit /b 1
)

pushd "%SCRIPT_DIR%" >nul
"%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%START_SCRIPT%" %GUI_FLAG% %*
set "EXIT_CODE=%ERRORLEVEL%"
popd >nul

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [mara] Launcher exited with code %EXIT_CODE%.
    echo [mara] Check logs in logs\ for details.
    pause
)

exit /b %EXIT_CODE%
