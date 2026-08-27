@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if "%~1"=="" (
  echo Usage:
  echo RUN_V55_CERTIFICATION.bat --parent-zip PATH --provider-dir PATH --ticks PATH --signals PATH --account-info PATH --symbol-info PATH --probe PATH --semantic-labels PATH --semantic-predictions PATH --trades PATH --out-dir V55_CERTIFICATION_OUTPUT
  exit /b 2
)
%PY% v55_orchestrator.py %*
exit /b %errorlevel%
