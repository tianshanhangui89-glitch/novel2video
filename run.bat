@echo off
setlocal
cd /d "%~dp0"

REM Windows: system proxy (e.g. 127.0.0.1:7897) must not intercept localhost.
set "NO_PROXY=127.0.0.1,localhost,::1"
set "no_proxy=127.0.0.1,localhost,::1"
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

if "%PORT%"=="" set "PORT=43123"

echo Novel2Video on http://127.0.0.1:%PORT%
echo Tip: if ComfyUI 8188 is reserved by Windows, use another port (e.g. 8288) in settings.
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
endlocal
