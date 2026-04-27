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
pip install -e '.[dev]'
```

## 本地启动（macOS/Linux）

1. 准备环境变量：

```bash
cp .env.example .env
```

编辑 `.env`，至少确认以下值：

- `SCRAPER_SERVER_BASE_URL`
- `SCRAPER_INTERNAL_API_KEY`
- `SCRAPER_CLIENT_ID`
- `PLAYWRIGHT_CDP_URL`

2. 启动带远程调试端口的 Chrome（示例端口 9222）：

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
```

3. 启动客户端（持续轮询任务）：

```bash
.venv/bin/scraper-client start
```

也可以用模块方式启动：

```bash
.venv/bin/python -m scraper_client.app.main start
```

说明：当可执行程序在“无命令行参数”启动时，会默认进入 `start` 模式。

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
2. Run workflow `Build Windows Executable` manually (`workflow_dispatch`) and choose `package_target`:
	- `test`: build package that reads `.env.test`
	- `prod`: build package that reads `.env.prod`
	- `both`: build both package variants
3. Wait for the build to finish and download the artifact.

You can also trigger this workflow by pushing a tag like `v0.1.0`.

Naming convention:
- `scraper-client-{version}-{platform}-{arch}-{target}`
- Example: `scraper-client-1.0.1-windows-x86_64-test`

GitHub Actions artifact name also includes the target suffix:
- `scraper-client-{version}-windows-x86_64-test`
- `scraper-client-{version}-windows-x86_64-prod`

Package layout in artifact:
- `scraper-client-{version}-windows-x86_64-{target}/scraper-client.exe`
- `scraper-client-{version}-windows-x86_64-{target}/.env.{target}`
- `scraper-client-{version}-windows-x86_64-{target}/.package-env`
- `scraper-client-{version}-windows-x86_64-{target}/.env.example`

`scraper-client.exe` will auto-detect `.package-env` and load `.env.test` or `.env.prod` accordingly.

### Run on Windows

1. Extract the GitHub artifact.
2. Open folder `scraper-client-{version}-windows-x86_64-{target}`.
3. Edit `.env.test` or `.env.prod` in this folder (based on package target).
4. Configure environment variables if you do not use package env files:
	- `SCRAPER_SERVER_BASE_URL` (example: `http://127.0.0.1:8000/api/v1`)
	- `SCRAPER_INTERNAL_API_KEY`
	- `PLAYWRIGHT_CDP_URL` (example: `http://127.0.0.1:9222`)
	- `SCRAPER_CLIENT_ID` (example: `windows-machine-01`)
5. Start client:

```powershell
.\scraper-client-{version}-windows-x86_64-{target}\scraper-client.exe
```

也可以双击 `scraper-client.exe` 启动（无参数时默认进入 `start` 模式）。

命令行方式启动（效果相同）：

```powershell
.\scraper-client-{version}-windows-x86_64-{target}\scraper-client.exe start
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
5. `ModuleNotFoundError: No module named 'scraper_client'`
	- Usually caused by using a global binary like `/opt/homebrew/bin/scraper-client` instead of this project's virtualenv.
	- Re-activate venv and reinstall: `source .venv/bin/activate && pip install -e '.[dev]'`.
	- Verify binary path: `which scraper-client` should point to `.venv/bin/scraper-client`.
