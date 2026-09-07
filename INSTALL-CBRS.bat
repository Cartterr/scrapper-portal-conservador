@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Instalador nativo - Plataforma CBRS
cd /d "%~dp0"

if /I "%~1"=="--plan" goto :plan

fltmc.exe >nul 2>&1
if errorlevel 1 (
    echo Solicitando permisos de administrador...
    set "CBRS_INSTALLER=%~f0"
    set "CBRS_INSTALL_DIR=%~dp0"
    powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command ^
      "Start-Process -FilePath $env:CBRS_INSTALLER -ArgumentList '--elevated' -WorkingDirectory $env:CBRS_INSTALL_DIR -Verb RunAs"
    if errorlevel 1 (
        echo No se concedieron permisos de administrador.
        pause
        exit /b 1
    )
    exit /b 0
)

:install
echo El instalador configurara el runtime CBRS completamente nativo en Windows.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ^
  "%~dp0deploy\windows\Install-CbrsNative.ps1" ^
  -RepoRoot "%~dp0."
set "CBRS_EXIT=%ERRORLEVEL%"
echo.
if "%CBRS_EXIT%"=="0" (
    echo El instalador termino correctamente.
) else if "%CBRS_EXIT%"=="3010" (
    echo La instalacion continuara despues de reiniciar Windows.
) else (
    echo El instalador termino con errores. Revise el mensaje y el log mostrado.
)
pause
exit /b %CBRS_EXIT%

:plan
echo Mostrando el plan de instalacion sin realizar cambios.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ^
  "%~dp0deploy\windows\Install-CbrsNative.ps1" ^
  -RepoRoot "%~dp0." -PlanOnly
set "CBRS_EXIT=%ERRORLEVEL%"
pause
exit /b %CBRS_EXIT%
