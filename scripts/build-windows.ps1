# Build a versioned Windows package for dianshang-scraper-c.
# Usage: .\scripts\build-windows.ps1 [-Target test|prod|both]  (default: both)
param(
    [ValidateSet("test","prod","both")]
    [string]$Target = "both"
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Push-Location $RootDir

try {
    # ── 1. Read version from pyproject.toml ──────────────────────────────────
    $version = (python -c "
from pathlib import Path
import tomllib
data = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
print(data['project']['version'])
").Trim()
    if (-not $version) {
        Write-Error "[build] ERROR: could not parse version from pyproject.toml"
        exit 1
    }
    Write-Host "[build] version = $version"

    # ── 2. Re-install editable package ───────────────────────────────────────
    Write-Host "[build] pip install -e ."
    .venv\Scripts\python.exe -m pip install -e . -q

    # ── 3. Determine targets ──────────────────────────────────────────────────
    $targets = switch ($Target) {
        "test" { @("test") }
        "prod" { @("prod") }
        "both" { @("test","prod") }
    }

    $arch = if ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture -eq "Arm64") { "arm64" } else { "x64" }
    $platform = "windows"

    # ── 4. Build executable ───────────────────────────────────────────────────
    Write-Host "[build] pyinstaller ..."
    .venv\Scripts\pyinstaller.exe `
        --onefile --console `
        --name scraper-client `
        src\scraper_client\app\main.py `
        --distpath .\dist `
        --workpath .\build `
        --copy-metadata dianshang-scraper-c `
        --noconfirm

    # ── 5. Stage versioned packages ───────────────────────────────────────────
    foreach ($tgt in $targets) {
        $envFile = ".env.$tgt"
        if (-not (Test-Path $envFile)) {
            Write-Error "[build] ERROR: missing $envFile"
            exit 1
        }

        $pkgDir = "staging\scraper-client-${version}-${platform}-${arch}-${tgt}"
        Write-Host "[build] staging → $pkgDir"
        if (Test-Path $pkgDir) { Remove-Item -Recurse -Force $pkgDir }
        New-Item -ItemType Directory -Path $pkgDir | Out-Null

        Copy-Item "dist\scraper-client.exe"  "$pkgDir\scraper-client-${tgt}.exe"
        Copy-Item ".env.example"             "$pkgDir\.env.example"
        Copy-Item ".env.example"             "$pkgDir\env.example"
        Copy-Item $envFile                   "$pkgDir\$envFile"
        Copy-Item $envFile                   "$pkgDir\env.$tgt"
        Set-Content -NoNewline -Path "$pkgDir\.package-env"  $tgt
        Set-Content -NoNewline -Path "$pkgDir\package-env"   $tgt
    }

    # ── 6. Smoke check ────────────────────────────────────────────────────────
    foreach ($tgt in $targets) {
        $pkgDir = "staging\scraper-client-${version}-${platform}-${arch}-${tgt}"
        $exe    = "$pkgDir\scraper-client-${tgt}.exe"
        Write-Host "[build] smoke check: $exe --help"
        & $exe --help

        $env:SCRAPER_SERVER_BASE_URL   = "http://127.0.0.1:8000/api/v1"
        $env:SCRAPER_INTERNAL_API_KEY  = "ci-smoke-key"
        $env:PLAYWRIGHT_CDP_URL        = "http://127.0.0.1:9222"
        $env:SCRAPER_SKIP_BACKEND_CHECK = "1"
        & $exe "start-$tgt" --help
    }

    Write-Host "[build] Done. Packages in staging\"
} finally {
    Pop-Location
}
