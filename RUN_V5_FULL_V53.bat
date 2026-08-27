@echo off
setlocal
cd /d "%~dp0"
title XAUUSD Provider-Faithful V5.3

echo ============================================================
echo XAUUSD Provider-Faithful V5.3 Execution-Integrity Replay
echo ============================================================
echo.
echo Primary policy:
echo   - 0.01 tickets only
echo   - max 3 tickets PER ROUND
echo   - no account-wide 3-ticket cap
echo   - open + pending downside stop risk <= 10%% equity
echo   - no fixed pending TTL
echo   - CEIL_HALF partial-close rounding
echo   - exact provider entries preferred when explicit
echo.

echo [1/3] Compiling V5.3 files...
python -m py_compile "%~dp0v53_policy.py" "%~dp0v53_engine.py" "%~dp0replay_blueberry_telegram_v5_3.py"
if errorlevel 1 goto :FAIL

echo [2/3] Running V5.3 semantic self-tests...
python "%~dp0test_v53_policy.py"
if errorlevel 1 goto :FAIL

echo [3/3] Starting full V5.3 tick replay...
python "%~dp0replay_blueberry_telegram_v5_3.py"
if errorlevel 1 goto :FAIL

echo.
echo ============================================================
echo V5.3 COMPLETE
echo Upload this file to ChatGPT:
echo Desktop\XAUUSD_BLUEBERRY_PROVIDER_FAITHFUL_V5_3_RESULTS.zip
echo ============================================================
pause
exit /b 0

:FAIL
echo.
echo ============================================================
echo V5.3 FAILED.
echo Send the full error shown above to ChatGPT.
echo ============================================================
pause
exit /b 1
