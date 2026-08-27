@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% -m compileall -q . || goto :fail
%PY% -m unittest -v test_v55_certification.py test_v55_hardening.py || goto :fail
echo.
echo V5.5 SELF-TEST: PASS
exit /b 0
:fail
echo.
echo V5.5 SELF-TEST: FAIL
exit /b 1
