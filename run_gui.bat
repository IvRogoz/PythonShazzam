@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv312\Scripts\python.exe"

if exist "%VENV_PY%" (
  call :ensure_deps || goto :fail
  "%VENV_PY%" "%ROOT%gui.py"
) else (
  echo Creating Python 3.12 virtual environment...
  py -3.12 -m venv "%ROOT%.venv312" || goto :fail

  call :install_deps || goto :fail
  "%VENV_PY%" "%ROOT%gui.py"
)

endlocal
exit /b 0

:ensure_deps
"%VENV_PY%" -c "from PIL import Image, ImageTk; import shazamio, mutagen, soundcard, sounddevice" >nul 2>&1 && exit /b 0
echo Missing dependencies detected. Repairing local environment...
call :install_deps
exit /b %errorlevel%

:install_deps
echo Installing dependencies...
"%VENV_PY%" -m pip install --no-user --upgrade pip || exit /b 1
"%VENV_PY%" -m pip install --no-user -r "%ROOT%requirements.txt" || exit /b 1
exit /b 0

:fail
echo.
echo Failed to prepare PythonShazzam GUI environment.
echo Make sure Python 3.12 is installed and available as: py -3.12
endlocal
exit /b 1
