@echo off
title Select ^& Play installer
echo Starting the Select ^& Play installer...
rem Double-click this to install (or update) the jukebox.
if exist "%~dp0install.ps1" goto local
rem Opened on its own or straight from the zip: fetch the installer from GitHub.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path $env:TEMP 'sap-install.ps1'; Invoke-WebRequest 'https://raw.githubusercontent.com/BrightonChappy/select-and-play-jukebox/main/install.ps1' -OutFile $p -UseBasicParsing; & $p; exit $LASTEXITCODE"
goto done
:local
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
:done
rem If anything went wrong, keep this window open so it can be read.
if errorlevel 1 (
  echo.
  echo The installer stopped. A record of what happened is in:
  echo %TEMP%\SelectAndPlay-install-log.txt
  pause
)
