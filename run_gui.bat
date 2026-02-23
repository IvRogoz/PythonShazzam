@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv312\Scripts\python.exe"

if exist "%VENV_PY%" (
  "%VENV_PY%" "%ROOT%gui.py"
) else (
  echo Creating Python 3.12 virtual environment...
  py -3.12 -m venv "%ROOT%.venv312" || goto :fail

  echo Installing dependencies...
  "%VENV_PY%" -m pip install --upgrade pip || goto :fail
  "%VENV_PY%" -m pip install -r "%ROOT%requirements.txt" || goto :fail

  "%VENV_PY%" "%ROOT%gui.py"
)

endlocal
exit /b 0

:fail
echo.
echo Failed to prepare PythonShazzam GUI environment.
echo Make sure Python 3.12 is installed and available as: py -3.12
endlocal
exit /b 1
