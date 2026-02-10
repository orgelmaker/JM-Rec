@echo off
chcp 65001 >nul 2>&1
setlocal
title JM-Rec Build
cd /d "%~dp0.."

echo.
echo  JM-Rec Build Script
echo  ====================
echo.

:: ─── Stap 1: PyInstaller ───
echo  [1/3] Exe bouwen met PyInstaller...
python -m PyInstaller JM-Rec.spec --noconfirm --clean >nul 2>&1
if not exist "dist\JM-Rec.exe" (
    echo  [FOUT] PyInstaller build mislukt.
    pause
    exit /b 1
)
echo         dist\JM-Rec.exe aangemaakt.

:: ─── Stap 2: Bestanden kopieren naar dist ───
echo  [2/3] Bestanden kopieren...
copy /Y "README.md" "dist\README.md" >nul
copy /Y "jm_rec_icon.ico" "dist\jm_rec_icon.ico" >nul
copy /Y "setup\jm_rec_setup.iss" "dist\jm_rec_setup.iss" >nul
echo         README, icon en ISS gekopieerd naar dist.

:: ─── Stap 3: Inno Setup ───
echo  [3/3] Installer bouwen met Inno Setup...
set "ISCC="
where iscc >nul 2>&1 && set "ISCC=iscc"
if "%ISCC%"=="" if exist "C:\InnoSetup\ISCC.exe" set "ISCC=C:\InnoSetup\ISCC.exe"
if "%ISCC%"=="" if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"

if "%ISCC%"=="" (
    echo  [FOUT] Inno Setup niet gevonden. Installeer Inno Setup 6.
    pause
    exit /b 1
)

"%ISCC%" "dist\jm_rec_setup.iss" >nul 2>&1
if not exist "output\JM-Rec-Setup.exe" (
    echo  [FOUT] Inno Setup build mislukt.
    pause
    exit /b 1
)
echo         output\JM-Rec-Setup.exe aangemaakt.

:: ─── Opruimen ───
echo.
echo  Opruimen...
rmdir /s /q "build" >nul 2>&1
rmdir /s /q "dist" >nul 2>&1
echo         build en dist verwijderd.

echo.
echo  ========================================
echo    Build voltooid!
echo    output\JM-Rec-Setup.exe is klaar.
echo  ========================================
echo.
pause
