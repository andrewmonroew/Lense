@echo off
REM ---------------------------------------------------------------------------
REM  Builds Lense.exe on Windows.
REM
REM  Run from the project root in a Command Prompt:  build_windows.bat
REM  The result is dist\Lense.exe -- a single self-contained file. It bundles the
REM  equipment catalog, icons and splash chime, so it needs nothing beside it.
REM ---------------------------------------------------------------------------
setlocal

echo.
echo === Lense Windows build ===
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python is not on PATH.
    echo Install Python 3.10+ from python.org and tick "Add python.exe to PATH".
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version') do echo Using Python %%v

echo.
echo --- Installing dependencies ---
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency install failed.
    exit /b 1
)

echo.
echo --- Clearing previous build ---
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo --- Running PyInstaller ---
python -m PyInstaller --noconfirm Lense.spec
if errorlevel 1 (
    echo ERROR: build failed.
    exit /b 1
)

echo.
if exist dist\Lense.exe (
    REM The quick-start sheet rides ALONGSIDE the exe rather than inside it --
    REM bundled into the exe it would unpack to a temp folder nobody ever opens.
    copy /y "READ ME FIRST.txt" dist\ >nul
    echo === Done ===
    for %%A in (dist\Lense.exe) do echo Built dist\Lense.exe  (%%~zA bytes^)
    echo Run it, or copy that single file anywhere.
    echo Hand out dist\"READ ME FIRST.txt" with it.
) else (
    echo ERROR: PyInstaller reported success but dist\Lense.exe is missing.
    exit /b 1
)

endlocal
