@echo off
setlocal ENABLEDELAYEDEXPANSION

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "APP_ROOT=%%~fI"
if "%APP_ROOT:~-1%"=="\" set "APP_ROOT=%APP_ROOT:~0,-1%"

if exist "%APP_ROOT%\.env" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%APP_ROOT%\.env") do (
    if not "%%A"=="" (
      set "K=%%A"
      if not "!K:~0,1!"=="#" (
        set "V=%%B"
        set "!K!=!V!"
      )
    )
  )
)

set "CHECK_ONLY=0"
if /I "%~1"=="--check-only" (
  set "CHECK_ONLY=1"
)

set "ERR=0"

if "%SCRAPER_SERVER_BASE_URL%"=="" (
  echo [ERROR] SCRAPER_SERVER_BASE_URL is empty.
  set "ERR=1"
)

if "%SCRAPER_INTERNAL_API_KEY%"=="" (
  echo [ERROR] SCRAPER_INTERNAL_API_KEY is empty.
  set "ERR=1"
) else (
  if /I "%SCRAPER_INTERNAL_API_KEY%"=="change-me-scraper-key" (
    echo [ERROR] SCRAPER_INTERNAL_API_KEY still uses default placeholder.
    set "ERR=1"
  )
)

if "%PLAYWRIGHT_CDP_URL%"=="" (
  echo [ERROR] PLAYWRIGHT_CDP_URL is empty.
  set "ERR=1"
)

if "%ERR%"=="1" goto :end

if not exist "%APP_ROOT%\logs" mkdir "%APP_ROOT%\logs"
set "LOG_PROBE=%APP_ROOT%\logs\.write_probe.tmp"
2>nul (echo probe>"%LOG_PROBE%")
if errorlevel 1 (
  echo [ERROR] logs directory is not writable: %APP_ROOT%\logs
  set "ERR=1"
) else (
  del /f /q "%LOG_PROBE%" >nul 2>nul
)
if "%ERR%"=="1" goto :end

echo [CHECK] backend reachability...
if /I "%SCRAPER_SKIP_BACKEND_CHECK%"=="1" (
  echo [WARN] backend check skipped because SCRAPER_SKIP_BACKEND_CHECK=1
) else (
  powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $base=$env:SCRAPER_SERVER_BASE_URL.TrimEnd('/'); $uri=$base + '/internal/scraper/task'; $headers=@{'X-Scraper-Key'=$env:SCRAPER_INTERNAL_API_KEY}; $r=Invoke-WebRequest -UseBasicParsing -Method Get -Uri $uri -Headers $headers -TimeoutSec 12; if ($r.StatusCode -lt 200 -or $r.StatusCode -ge 500) { throw 'unexpected status: ' + $r.StatusCode }"
  if errorlevel 1 (
    echo [ERROR] backend not reachable or scraper key rejected.
    set "ERR=1"
    goto :end
  )
)

echo [CHECK] cdp reachability...
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $u=$env:PLAYWRIGHT_CDP_URL.TrimEnd('/'); $r=Invoke-WebRequest -UseBasicParsing -Method Get -Uri ($u + '/json/version') -TimeoutSec 10; if ($r.StatusCode -ne 200) { throw 'unexpected status: ' + $r.StatusCode }"
if errorlevel 1 (
  echo [ERROR] CDP endpoint is not reachable: %PLAYWRIGHT_CDP_URL%
  set "ERR=1"
  goto :end
)

echo [OK] prechecks passed.

if "%CHECK_ONLY%"=="1" goto :end

pushd "%APP_ROOT%"
set "EXE=%APP_ROOT%\scraper-client.exe"
if exist "%EXE%" (
  echo [RUN] %EXE% start
  "%EXE%" start
  set "RUN_EXIT=%ERRORLEVEL%"
  popd
  exit /b %RUN_EXIT%
)

echo [RUN] python -m scraper_client.app.main start
python -m scraper_client.app.main start
set "RUN_EXIT=%ERRORLEVEL%"
popd
exit /b %RUN_EXIT%

:end
if not "%ERR%"=="0" (
  echo [FAIL] startup precheck failed.
)
exit /b %ERR%
