@echo off
REM Convenience shim: forwards to launcher\start.ps1 (Windows daily launcher).
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\start.ps1"
endlocal
