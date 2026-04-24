# dianshang-scraper-c

CDP-only scraper client for the dianshang backend internal scraper APIs.

## Features

- Pull active shop accounts from backend
- Execute scraping via CDP-connected browser
- Upload orders/items/price_info results
- Upload run logs with order_count

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### Required environment variables

- `SCRAPER_SERVER_BASE_URL` (example: `http://127.0.0.1:8000/api/v1`)
- `SCRAPER_INTERNAL_API_KEY`
- `PLAYWRIGHT_CDP_URL` (example: `http://127.0.0.1:9222`)
- `SCRAPER_CLIENT_ID` (example: `macbook-pro-01`)

## Start (continuous)

```bash
scraper-client start
```

Behavior:
- The client runs continuously.
- Each cycle pulls active accounts from backend and executes them.
- Platform is decided by each account's platform field.
- Stop with Ctrl+C or SIGTERM; the current account is finalized before exit.

## Release

### Build Windows executable

1. Open GitHub Actions.
2. Run workflow `Build Windows Executable` manually (`workflow_dispatch`).
3. Wait for the build to finish and download the artifact.

You can also trigger this workflow by pushing a tag like `v0.1.0`.

Naming convention:
- `scraper-client-{version}-{platform}-{arch}`
- Example: `scraper-client-0.1.0-windows-x86_64.zip`

Package layout in artifact:
- `staging/scraper-client-{version}-windows-x86_64/scraper-client.exe`

### Run on Windows

1. Extract `scraper-client-{version}-windows-x86_64.zip`.
2. Configure environment variables:
	- `SCRAPER_SERVER_BASE_URL` (example: `http://127.0.0.1:8000/api/v1`)
	- `SCRAPER_INTERNAL_API_KEY`
	- `PLAYWRIGHT_CDP_URL` (example: `http://127.0.0.1:9222`)
	- `SCRAPER_CLIENT_ID` (example: `windows-machine-01`)
3. Start client:

```powershell
.\staging\scraper-client-{version}-windows-x86_64\scraper-client.exe start
```
