from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from scraper_client.core.settings import Settings
from scraper_client.infra.browser.chrome_discovery import ChromeCdpResolver

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class JDBrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    storage_state_path: Path


class JDSessionManager:
    """Manage CDP browser session and persisted storage state per account."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._resolver = ChromeCdpResolver(
            preferred_url=settings.playwright_cdp_url,
            timeout_ms=settings.playwright_timeout_ms,
            client_id=settings.scraper_client_id,
        )

    def open_session(self, account_id: int) -> JDBrowserSession:
        cdp_url = self._resolver.resolve()
        playwright = sync_playwright().start()
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=self._settings.playwright_timeout_ms)
        # Always create an isolated context per account to avoid cookie/state pollution.
        context = browser.new_context()

        storage_state_path = Path("artifacts") / "storage" / f"jd_account_{account_id}.json"
        if self._settings.force_relogin_each_run:
            logger.info(
                "force_relogin_each_run enabled, skip loading storage state account_id=%s",
                account_id,
            )
        else:
            self._load_storage_state(context, storage_state_path)

        page = context.new_page()
        logger.info("opened isolated JD browser session account_id=%s storage_state=%s", account_id, storage_state_path)
        return JDBrowserSession(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            storage_state_path=storage_state_path,
        )

    def close_session(self, session: JDBrowserSession, *, persist_state: bool = True) -> None:
        if persist_state:
            self._save_storage_state(session.context, session.storage_state_path)
        try:
            session.context.close()
        except Exception:
            logger.debug("failed to close browser context", exc_info=True)
        try:
            session.browser.close()
        except Exception:
            logger.debug("failed to close browser connection", exc_info=True)
        try:
            session.playwright.stop()
        except Exception:
            logger.debug("failed to stop playwright", exc_info=True)

    def _save_storage_state(self, context: BrowserContext, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(path))

    def _load_storage_state(self, context: BrowserContext, path: Path) -> None:
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cookies = payload.get("cookies")
            if isinstance(cookies, list) and cookies:
                context.add_cookies(cookies)
        except Exception:
            logger.warning("failed to load storage state file=%s", path, exc_info=True)
