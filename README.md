# dianshang-scraper-c

CDP-only scraper client for the dianshang backend internal scraper APIs.

## Features

- Pull active shop accounts from backend
- Execute scraping via CDP-connected browser
- Upload orders/items/price_info results
- Upload run logs with order_count
- Native JINGDONG pipeline (session manager, login fallback, order+item+price parsing)
- Persistent account session state under `artifacts/storage/`

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

Optional:

- `SCRAPER_SKIP_BACKEND_CHECK=1` (only for local smoke/startup check)

## Start (continuous)

```bash
scraper-client start
```

Behavior:
- The client runs continuously.
- Each cycle pulls one task from backend and executes it.
- Platform is decided by each account's platform field.
- CDP connection is enabled by default: it first tries `PLAYWRIGHT_CDP_URL`; if unreachable, it auto-discovers local Chrome CDP endpoints and then auto-starts Chrome when needed.
- JINGDONG login strategy:
	- First run: supports manual login in browser and saves session to `artifacts/storage/jd_account_{id}.json`.
	- Later runs: reuses persisted session automatically.
	- Session invalid: raises explicit error and requires manual intervention.
- Scrape config delivered by server is respected: `human_action_min_ms`, `human_action_max_ms`, `scrape_max_pages`.
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
- `staging/scraper-client-{version}-windows-x86_64/start.bat`
- `staging/scraper-client-{version}-windows-x86_64/.env.example`

### Run on Windows

1. Extract `scraper-client-{version}-windows-x86_64.zip`.
2. Copy `.env.example` to `.env` and fill values.
3. Configure environment variables if you do not use `.env`:
	- `SCRAPER_SERVER_BASE_URL` (example: `http://127.0.0.1:8000/api/v1`)
	- `SCRAPER_INTERNAL_API_KEY`
	- `PLAYWRIGHT_CDP_URL` (example: `http://127.0.0.1:9222`)
	- `SCRAPER_CLIENT_ID` (example: `windows-machine-01`)
4. Start client with precheck:

```powershell
.\staging\scraper-client-{version}-windows-x86_64\start.bat
```

`start.bat` performs prechecks before start:

- Backend endpoint reachable and `X-Scraper-Key` accepted.
- `PLAYWRIGHT_CDP_URL` reachable (`/json/version`).
- Log directory writable.

Check-only mode (no daemon start):

```powershell
.\staging\scraper-client-{version}-windows-x86_64\start.bat --check-only
```

Runtime logs are written to:

```text
./logs/{SCRAPER_CLIENT_ID}.log
```

## Log Upload / Run Log Contract

For each task execution:

- Successful scrape uploads `/internal/scraper/results/upload` first.
- Then uploads `/internal/scraper/logs/upload` with `run_status=SUCCESS`.
- If scraping fails, it still uploads `/internal/scraper/logs/upload` with `run_status=FAILED` and `error_message`.

This behavior is implemented in `AccountOrchestrator.execute_task` and keeps backend run-history complete.

## Troubleshooting

1. `backend not reachable or scraper key rejected`
	- Verify `SCRAPER_SERVER_BASE_URL` and `SCRAPER_INTERNAL_API_KEY`.
	- Check backend route `/api/v1/internal/scraper/task` and key middleware.
2. `CDP endpoint is not reachable`
	- Start Chrome with remote debugging port 9222 or set `PLAYWRIGHT_CDP_URL` to active endpoint.
3. `session expired, manual login required`
	- Re-run, complete JD login in browser, then client saves new session state automatically.
4. `orders extracted but items/price_info empty`
	- Keep browser on JD order pages and avoid extensions blocking network requests.
	- Increase `scrape_max_pages` from backend account config.
