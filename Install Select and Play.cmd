@echo off
title Select ^& Play installer
rem Double-click this to install (or update) the jukebox.
if exist "%~dp0install.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
) else (
  rem opened on its own or straight from the zip: fetch the installer from GitHub
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path $env:TEMP 'sap-install.ps1'; Invoke-WebRequest 'https://raw.githubusercontent.com/BrightonChappy/select-and-play-jukebox/main/install.ps1' -OutFile $p -UseBasicParsing; & $p"
)
