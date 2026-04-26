from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from typing import Any

from playwright.sync_api import Page

from scraper_client.core.settings import Settings, get_settings
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

# 行为验证码（滑块/图形验证）特征
_BEHAVIOR_CAPTCHA_SELECTORS = [
    ".verify-slide",
    ".shumei-captcha",
    ".jd-captcha-wrap",
    "#captcha-container",
    ".captcha-box",
    "#jcap_holder",
    ".JDJRV-bigimg",
]
_BEHAVIOR_CAPTCHA_TEXT = ["请完成验证", "滑动验证", "安全验证", "拖动滑块"]

# 短信/手机验证码特征
_SMS_VERIFICATION_SELECTORS = [
    "#authcode",
    "input[placeholder*='验证码']",
    "input[placeholder*='短信']",
    "input[name='authcode']",
    ".sms-captcha-input",
]
_SMS_VERIFICATION_TEXT = ["短信验证码", "手机验证码", "获取验证码"]


class JDAuthenticator:
    """Ensure JD account has a valid authenticated browser session."""

    def __init__(self, session_manager: JDSessionManager, settings: Settings | None = None) -> None:
        self._session_manager = session_manager
        self._settings = settings or get_settings()

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

        # Try credential-based login before falling back to manual login.
        if self._try_auto_login(page, account=account, wait_timeout_seconds=wait_timeout_seconds):
            self._session_manager._save_storage_state(session.context, session.storage_state_path)
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
            self._handle_verification_if_present(page, account=account)
            page.wait_for_timeout(1500)

        raise JDLoginError(
            "merchant login timeout — please open Chrome, log in to shop.jd.com, then re-run"
        )

    def _try_auto_login(
        self,
        page: Page,
        *,
        account: dict[str, Any],
        wait_timeout_seconds: int,
    ) -> bool:
        username = str(account.get("account_name") or "").strip()
        password = str(account.get("account_password") or "").strip()
        if not username or not password:
            logger.info("skip auto login: missing account_name/account_password")
            return False

        logger.info("attempting credential login account_id=%s", account.get("id"))
        page.goto(JD_MERCHANT_LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)

        if not self._is_login_required(page):
            return True

        self._switch_to_password_login(page)

        username_selectors = [
            "input#loginname",
            "input[name='loginname']",
            "input[placeholder*='账号']",
            "input[placeholder*='用户名']",
            "input[type='text']",
        ]
        password_selectors = [
            "input#nloginpwd",
            "input[type='password']",
            "input[name='nloginpwd']",
            "input[name='password']",
        ]
        submit_selectors = [
            "#loginsubmit",
            "button#loginsubmit",
            "button.login-btn",
            "button:has-text('登录')",
            "a:has-text('登录')",
            ".login-btn",
        ]

        if not self._fill_first_available(page, username_selectors, username):
            logger.warning("auto login failed: username input not found")
            return False
        if not self._fill_first_available(page, password_selectors, password):
            logger.warning("auto login failed: password input not found")
            return False

        clicked = self._click_first_available(page, submit_selectors)
        if not clicked:
            # Last resort: submit from password input.
            if not self._press_enter_first_available(page, password_selectors):
                logger.warning("auto login failed: submit control not found")
                return False

        deadline = time.time() + max(20, int(wait_timeout_seconds))
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            self._handle_verification_if_present(page, account=account)
            if not self._is_login_required(page):
                page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
                if not self._is_login_required(page):
                    logger.info("credential login succeeded account_id=%s", account.get("id"))
                    return True

        logger.warning("credential login timed out account_id=%s", account.get("id"))
        return False

    @staticmethod
    def _switch_to_password_login(page: Page) -> None:
        selectors = [
            ".login-tab-r",
            "text=账户登录",
            "text=账号登录",
            "text=密码登录",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    locator.first.click(timeout=1500)
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    @staticmethod
    def _fill_first_available(page: Page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                target = locator.first
                target.click(timeout=1500)
                target.fill(value, timeout=3000)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _click_first_available(page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=2000)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _press_enter_first_available(page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                locator.first.press("Enter", timeout=1000)
                return True
            except Exception:
                continue
        return False

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

    # ------------------------------------------------------------------
    # 行为验证码 / 短信验证码检测 & 通知
    # ------------------------------------------------------------------

    def _handle_verification_if_present(
        self,
        page: Page,
        *,
        account: dict[str, Any],
    ) -> None:
        """检测行为验证码或短信验证码，发现时发送邮件通知（每类最多通知一次/轮）。"""
        if self._detect_behavior_captcha(page):
            logger.warning(
                "behavior captcha detected account_id=%s account_name=%s",
                account.get("id"),
                account.get("account_name"),
            )
            to = str(account.get("email") or "").strip()
            if to:
                self._send_email_notification(
                    to=to,
                    subject=f"[京东登录] 行为验证码需要处理 — {account.get('account_name')}",
                    body=(
                        f"账号 {account.get('account_name')} 在登录京东商家后台时遇到行为验证码（滑块验证），"
                        "请打开 Chrome 浏览器手动完成验证。\n\n"
                        f"账号 ID: {account.get('id')}"
                    ),
                )
        elif self._detect_sms_verification(page):
            logger.warning(
                "SMS verification code required account_id=%s account_name=%s",
                account.get("id"),
                account.get("account_name"),
            )
            to = str(account.get("email") or "").strip()
            if to:
                self._send_email_notification(
                    to=to,
                    subject=f"[京东登录] 需要输入短信验证码 — {account.get('account_name')}",
                    body=(
                        f"账号 {account.get('account_name')} 在登录京东商家后台时需要输入短信验证码，"
                        "请查收手机短信并在 Chrome 浏览器中填写验证码。\n\n"
                        f"账号 ID: {account.get('id')}"
                    ),
                )

    @staticmethod
    def _detect_behavior_captcha(page: Page) -> bool:
        """返回 True 表示页面上存在行为验证码弹窗/组件。"""
        for selector in _BEHAVIOR_CAPTCHA_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        try:
            body_text = page.inner_text("body", timeout=2000)
            if any(mark in body_text for mark in _BEHAVIOR_CAPTCHA_TEXT):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _detect_sms_verification(page: Page) -> bool:
        """返回 True 表示页面上存在短信/手机验证码输入框。"""
        for selector in _SMS_VERIFICATION_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        try:
            body_text = page.inner_text("body", timeout=2000)
            if any(mark in body_text for mark in _SMS_VERIFICATION_TEXT):
                return True
        except Exception:
            pass
        return False

    def _send_email_notification(self, *, to: str, subject: str, body: str) -> None:
        """通过 SMTP 发送邮件通知；配置不完整或发送失败时仅记录日志。"""
        cfg = self._settings
        if not (cfg.smtp_host and cfg.smtp_username and cfg.smtp_password):
            logger.debug("SMTP not configured, skip email notification to %s", to)
            return

        sender = cfg.smtp_from or cfg.smtp_username
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=15) as server:
                server.login(cfg.smtp_username, cfg.smtp_password)
                server.sendmail(sender, [to], msg.as_string())
            logger.info("email notification sent to %s subject=%r", to, subject)
        except Exception as exc:
            logger.warning("failed to send email notification to %s: %s", to, exc)
