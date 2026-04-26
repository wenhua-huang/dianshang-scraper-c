from __future__ import annotations

import logging
import time
from typing import Any

from playwright.sync_api import Page

from scraper_client.infra.jd.exceptions import JDLoginError, JDSessionExpiredError
from scraper_client.infra.jd.session_manager import JDBrowserSession, JDSessionManager

logger = logging.getLogger(__name__)

# 新版商家后台走 /jdm/trade/orders/ 微前端路由，默认会跳转到 waitOut tab；
# 认证检测只需成功加载页面，实际抓取时 extractor 自行导航到 allOrders tab。
JD_ORDER_LIST_URL = "https://shop.jd.com/jdm/trade/order/orderList"
# 商家后台登录入口（shop.jd.com 和消费者 passport 是两套 Cookie）
JD_MERCHANT_LOGIN_URL = "https://shop.jd.com/"

# 页面内容中出现这些文字说明商家后台 session 无效
_PAGE_NOT_FOUND_MARKS = [
    "此页面没有找到",
    "您访问的页面不存在",
    "抱歉主人",
    "自动为您转入",
]


class JDAuthenticator:
    """Ensure JD account has a valid authenticated browser session."""

    def __init__(self, session_manager: JDSessionManager) -> None:
        self._session_manager = session_manager

    def ensure_authenticated(
        self,
        session: JDBrowserSession,
        *,
        account: dict[str, Any],
        allow_manual_login: bool,
        wait_timeout_seconds: int,
    ) -> None:
        page = session.page

        page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
        if not self._is_login_required(page):
            return

        # Retry once with persisted state reloaded from disk.
        self._session_manager._load_storage_state(session.context, session.storage_state_path)
        page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
        if not self._is_login_required(page):
            return

        if not allow_manual_login:
            raise JDSessionExpiredError("session expired, manual login required")

        logger.warning(
            "JD merchant login required account_id=%s account_name=%s — "
            "please complete login at shop.jd.com in the opened browser window",
            account.get("id"),
            account.get("account_name"),
        )
        # 导航到商家后台首页让用户手动登录
        page.goto(JD_MERCHANT_LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)

        deadline = time.time() + max(30, int(wait_timeout_seconds))
        while time.time() < deadline:
            if not self._is_login_required(page):
                page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
                if not self._is_login_required(page):
                    self._session_manager._save_storage_state(session.context, session.storage_state_path)
                    return
            page.wait_for_timeout(1500)

        raise JDLoginError(
            "merchant login timeout — please open Chrome, log in to shop.jd.com, then re-run"
        )

    @staticmethod
    def _is_login_required(page: Page) -> bool:
        url = (page.url or "").lower()
        if "passport.jd.com" in url or "login" in url:
            return True

        # 商家后台「页面找不到」等同于 session 失效
        try:
            body_text = page.inner_text("body", timeout=3000)
            if any(mark in body_text for mark in _PAGE_NOT_FOUND_MARKS):
                return True
        except Exception:
            pass

        selectors = [
            "input#loginname",
            "input[name='loginname']",
            "input[type='password']",
            ".login-box",
            ".login-tab-r",
        ]
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False
