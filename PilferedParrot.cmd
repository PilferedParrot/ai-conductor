@echo off
setlocal
pushd "%~dp0"
set "PYTHON=py -3"
%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 goto :run
set "PYTHON=python"
%PYTHON% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto :missing
:run
%PYTHON% -m pilferedparrot.windows %*
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" (
  echo PilferedParrot failed with exit code %exit_code%. 1>&2
  pause
)
popd
exit /b %exit_code%
:missing
echo PilferedParrot requires Python 3.12 or newer. Install Python and try again. 1>&2
popd
pause
exit /b 1
