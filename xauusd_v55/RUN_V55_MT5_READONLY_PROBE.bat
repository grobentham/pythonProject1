@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% v55_mt5_readonly_probe.py --symbol XAUUSD.i --volume 0.01 --output V55_MT5_READ_ONLY_PROBE.json
if errorlevel 1 goto :fail
echo.
echo V5.5 MT5 READ-ONLY PROBE: COMPLETE
echo No order_send call exists in this probe.
exit /b 0
:fail
echo.
echo V5.5 MT5 READ-ONLY PROBE: FAILED
exit /b 1
