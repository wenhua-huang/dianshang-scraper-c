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
from scraper_client.infra.server.internal_api_client import InternalApiClient

logger = logging.getLogger(__name__)

# 新版商家后台走 /jdm/trade/orders/ 微前端路由，默认会跳转到 waitOut tab；
# 认证检测只需成功加载页面，实际抓取时 extractor 自行导航到 allOrders tab。
JD_ORDER_LIST_URL = "https://shop.jd.com/jdm/trade/orders/order-list?tabType=allOrders"
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
        self._api_client = InternalApiClient(
            base_url=self._settings.scraper_server_base_url,
            api_key=self._settings.scraper_internal_api_key,
        )
        self._last_email_sent_at: dict[str, float] = {}

    def ensure_authenticated(
        self,
        session: JDBrowserSession,
        *,
        account: dict[str, Any],
        allow_manual_login: bool,
        wait_timeout_seconds: int,
    ) -> None:
        page = session.page
        force_relogin = bool(self._settings.force_relogin_each_run)
        logger.info(
            "auth check start account_id=%s account_name=%s allow_manual_login=%s force_relogin=%s",
            account.get("id"),
            account.get("account_name"),
            allow_manual_login,
            force_relogin,
        )

        if force_relogin:
            try:
                session.context.clear_cookies()
                logger.info("force relogin: cleared context cookies account_id=%s", account.get("id"))
            except Exception:
                logger.warning(
                    "force relogin: failed to clear context cookies account_id=%s",
                    account.get("id"),
                    exc_info=True,
                )

        page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
        logger.info("auth check visited order-list target=%s current_url=%s", JD_ORDER_LIST_URL, page.url)
        if not force_relogin and not self._is_login_required(page):
            if self._is_order_list_page(page):
                logger.info(
                    "auth check passed by existing session account_id=%s no credential input needed",
                    account.get("id"),
                )
                return
            logger.warning(
                "session appears logged-in but current page is not order-list account_id=%s current_url=%s",
                account.get("id"),
                page.url,
            )

        if force_relogin and not self._is_login_required(page):
            logger.warning(
                "force relogin requested but session still appears logged in account_id=%s; "
                "will continue with credential login attempt",
                account.get("id"),
            )

        # Retry once with persisted state reloaded from disk.
        if not force_relogin:
            logger.info(
                "auth check failed first pass, reloading persisted storage_state account_id=%s",
                account.get("id"),
            )
            self._session_manager._load_storage_state(session.context, session.storage_state_path)
            page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
            if not self._is_login_required(page):
                if self._is_order_list_page(page):
                    logger.info(
                        "auth check passed after storage_state reload account_id=%s no credential input needed",
                        account.get("id"),
                    )
                    return
                logger.warning(
                    "storage-state reload still not at order-list page account_id=%s current_url=%s",
                    account.get("id"),
                    page.url,
                )

        # Try credential-based login before falling back to manual login.
        logger.info("auth check still requires login, trying credential login account_id=%s", account.get("id"))
        if self._try_auto_login(page, account=account, wait_timeout_seconds=wait_timeout_seconds):
            self._session_manager._save_storage_state(session.context, session.storage_state_path)
            logger.info("auth completed by credential login account_id=%s", account.get("id"))
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
                if not self._is_login_required(page) and self._is_order_list_page(page):
                    self._session_manager._save_storage_state(session.context, session.storage_state_path)
                    logger.info("auth completed by manual login account_id=%s", account.get("id"))
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

        logger.info(
            "attempting credential login account_id=%s username=%s",
            account.get("id"),
            username,
        )
        page.goto(JD_MERCHANT_LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)

        if not self._is_login_required(page):
            page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
            if not self._is_login_required(page) and self._is_order_list_page(page):
                return True

        self._switch_to_password_login(page)
        logger.info("switched to password login tab account_id=%s", account.get("id"))

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
        logger.info("typed username account_id=%s", account.get("id"))

        if not self._fill_first_available(page, password_selectors, password):
            logger.warning("auto login failed: password input not found")
            return False
        logger.info(
            "typed password account_id=%s password_length=%s",
            account.get("id"),
            len(password),
        )

        # Give JD login form a short settle window before submit click.
        page.wait_for_timeout(300)

        clicked = self._click_login_submit(page, submit_selectors)
        if not clicked:
            # Last resort: submit from password input.
            if not self._press_enter_first_available(page, password_selectors):
                logger.warning("auto login failed: submit control not found")
                return False
            logger.info("submitted login by Enter key account_id=%s", account.get("id"))
        else:
            logger.info("clicked login submit button account_id=%s", account.get("id"))

        # JD may switch into SMS verification after credential submit.
        if self._detect_sms_verification(page):
            logger.warning("sms verification detected after login submit account_id=%s", account.get("id"))
            if not self._handle_sms_verification(page, account=account):
                logger.warning("sms verification flow failed account_id=%s", account.get("id"))
                return False

        deadline = time.time() + max(20, int(wait_timeout_seconds))
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            self._handle_verification_if_present(page, account=account)
            if not self._is_login_required(page):
                page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
                if not self._is_login_required(page) and self._is_order_list_page(page):
                    logger.info("credential login succeeded account_id=%s", account.get("id"))
                    return True

        logger.warning("credential login timed out account_id=%s", account.get("id"))
        return False

    def _click_login_submit(self, page: Page, selectors: list[str]) -> bool:
        """Prefer explicit login button click; fall back to JS click if needed."""
        for selector in selectors:
            for target in self._iter_targets(page):
                try:
                    locator = target.locator(selector)
                    if locator.count() == 0:
                        continue

                    button = locator.first
                    for _ in range(2):
                        try:
                            button.click(timeout=2500)
                            return True
                        except Exception:
                            page.wait_for_timeout(200)

                    try:
                        button.evaluate("el => el.click()")
                        return True
                    except Exception:
                        continue
                except Exception:
                    continue
        return False

    def _handle_sms_verification(self, page: Page, *, account: dict[str, Any]) -> bool:
        account_id = int(account.get("id") or 0)
        if account_id <= 0:
            logger.warning("sms verification skipped: invalid account id")
            return False

        if self._fill_sms_phone(page, account=account):
            logger.info("typed sms phone number account_id=%s", account_id)
        else:
            logger.info("sms phone input skipped/not found account_id=%s", account_id)

        if not self._trigger_sms_send(page):
            logger.warning("sms verification trigger not found account_id=%s", account_id)
            return False
        logger.info("clicked send sms verification button account_id=%s", account_id)

        try:
            session = self._api_client.create_verification_session(account_id)
        except Exception:
            logger.exception("create verification session failed account_id=%s", account_id)
            return False

        request_id = str(session.get("request_id") or "").strip()
        if not request_id:
            logger.warning("verification session missing request_id account_id=%s", account_id)
            return False

        logger.info("verification session created account_id=%s request_id=%s", account_id, request_id)
        deadline = time.time() + int(self._settings.verification_max_wait_seconds)
        poll_seconds = float(self._settings.verification_poll_interval_seconds)

        while time.time() < deadline:
            try:
                status_payload = self._api_client.get_verification_status(request_id)
            except Exception:
                logger.exception("get verification status failed request_id=%s", request_id)
                page.wait_for_timeout(int(poll_seconds * 1000))
                continue

            status_text = str(status_payload.get("status") or "")
            code_available = bool(status_payload.get("code_available"))
            logger.info(
                "verification polling account_id=%s request_id=%s status=%s code_available=%s",
                account_id,
                request_id,
                status_text,
                code_available,
            )

            if status_text == "SUBMITTED" or code_available:
                try:
                    consumed = self._api_client.consume_verification_code(request_id)
                except Exception:
                    logger.exception("consume verification code failed request_id=%s", request_id)
                    return False

                code = str(consumed.get("code") or "").strip()
                if not code:
                    logger.warning("verification code empty request_id=%s", request_id)
                    return False
                logger.info("verification code received request_id=%s", request_id)
                return self._submit_code_to_page(page, code)

            if status_text in {"EXPIRED", "CONSUMED"}:
                logger.warning("verification session ended request_id=%s status=%s", request_id, status_text)
                return False

            page.wait_for_timeout(int(poll_seconds * 1000))

        logger.warning("verification wait timeout request_id=%s", request_id)
        return False

    @staticmethod
    def _fill_sms_phone(page: Page, *, account: dict[str, Any]) -> bool:
        raw_phone = str(account.get("phone") or "")
        digits = "".join(ch for ch in raw_phone if ch.isdigit())
        if len(digits) > 11 and digits.startswith("86"):
            digits = digits[-11:]
        if len(digits) < 11:
            return False

        selectors = [
            "input[placeholder*='手机号']",
            "input[name*='phone']",
            "input[type='tel']",
        ]
        for selector in selectors:
            for target in JDAuthenticator._iter_targets(page):
                try:
                    locator = target.locator(selector)
                    if locator.count() == 0:
                        continue
                    control = locator.first
                    control.click(timeout=1500)
                    control.fill(digits, timeout=3000)
                    return True
                except Exception:
                    continue
        return False

    def _trigger_sms_send(self, page: Page) -> bool:
        selectors = [
            "button:has-text('发送验证码')",
            "button:has-text('获取验证码')",
            "button:has-text('发送短信')",
            "button:has-text('获取短信')",
            "text=获取验证码",
            "text=发送验证码",
            "[class*='get-code']",
            "[class*='getCode']",
            "[class*='send-code']",
            "[class*='sendCode']",
        ]
        return self._click_first_available(page, selectors)

    @staticmethod
    def _submit_code_to_page(page: Page, code: str) -> bool:
        input_selectors = [
            "input[placeholder*='验证码']",
            "input[aria-label*='验证码']",
            "input[name*='code']",
            "input[name='authcode']",
            "input[type='tel']",
            "input[type='text']",
        ]
        submit_selectors = [
            "button:has-text('确定')",
            "button:has-text('登录')",
            "button:has-text('提交')",
            "button:has-text('验证')",
            "button:has-text('确认')",
        ]

        if not JDAuthenticator._fill_first_available(page, input_selectors, code):
            return False
        logger.info("typed sms verification code code_length=%s", len(code))
        if JDAuthenticator._click_first_available(page, submit_selectors):
            logger.info("submitted sms verification by button")
            return True
        submitted = JDAuthenticator._press_enter_first_available(page, input_selectors)
        if submitted:
            logger.info("submitted sms verification by Enter key")
        return submitted

    @staticmethod
    def _switch_to_password_login(page: Page) -> None:
        selectors = [
            ".login-tab-r",
            "text=账户登录",
            "text=账号登录",
            "text=密码登录",
        ]
        for selector in selectors:
            for target in JDAuthenticator._iter_targets(page):
                try:
                    locator = target.locator(selector)
                    if locator.count() > 0:
                        locator.first.click(timeout=1500)
                        page.wait_for_timeout(500)
                        return
                except Exception:
                    continue

    @staticmethod
    def _iter_targets(page: Page):
        """Yield main page first, then child frames for login controls inside iframe."""
        yield page
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            yield frame

    @staticmethod
    def _fill_first_available(page: Page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            for target in JDAuthenticator._iter_targets(page):
                try:
                    locator = target.locator(selector)
                    if locator.count() == 0:
                        continue
                    control = locator.first
                    control.click(timeout=1500)
                    control.fill(value, timeout=3000)
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _click_first_available(page: Page, selectors: list[str]) -> bool:
        for selector in selectors:
            for target in JDAuthenticator._iter_targets(page):
                try:
                    locator = target.locator(selector)
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
            for target in JDAuthenticator._iter_targets(page):
                try:
                    locator = target.locator(selector)
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
            for target in JDAuthenticator._iter_targets(page):
                try:
                    if target.locator(selector).count() > 0:
                        return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _is_order_list_page(page: Page) -> bool:
        url = (page.url or "").lower()
        if "/jdm/trade/orders/order-list" in url:
            return True

        selectors = [
            ".order-list-card-table",
            "[class*='order-list']",
            "a[href*='tabType=allOrders']",
        ]
        for selector in selectors:
            for target in JDAuthenticator._iter_targets(page):
                try:
                    if target.locator(selector).count() > 0:
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
                logger.info(
                    "sending behavior captcha alert email account_id=%s to=%s",
                    account.get("id"),
                    to,
                )
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
                logger.info(
                    "sending sms verification alert email account_id=%s to=%s",
                    account.get("id"),
                    to,
                )
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
                    logger.info("behavior captcha matched by selector=%s", selector)
                    return True
            except Exception:
                continue
        try:
            body_text = page.inner_text("body", timeout=2000)
            if any(mark in body_text for mark in _BEHAVIOR_CAPTCHA_TEXT):
                logger.info("behavior captcha matched by page text")
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
                    logger.info("sms verification matched by selector=%s", selector)
                    return True
            except Exception:
                continue
        try:
            body_text = page.inner_text("body", timeout=2000)
            if any(mark in body_text for mark in _SMS_VERIFICATION_TEXT):
                logger.info("sms verification matched by page text")
                return True
        except Exception:
            pass
        return False

    def _send_email_notification(self, *, to: str, subject: str, body: str) -> None:
        """通过 SMTP 发送邮件通知；配置不完整或发送失败时仅记录日志。"""
        cfg = self._settings
        if not (cfg.smtp_host and cfg.smtp_username and cfg.smtp_password):
            logger.info("SMTP not configured, skip email notification to=%s", to)
            return

        now = time.time()
        cooldown = max(0, int(cfg.captcha_alert_cooldown_seconds))
        cache_key = f"{to}:{subject}"
        last_sent_at = self._last_email_sent_at.get(cache_key)
        if last_sent_at is not None and now - last_sent_at < cooldown:
            logger.info(
                "email notification suppressed by cooldown to=%s subject=%r",
                to,
                subject,
            )
            return

        sender = cfg.smtp_from or cfg.smtp_username
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = to

        try:
            logger.info(
                "sending email notification to=%s subject=%r smtp_host=%s smtp_port=%s",
                to,
                subject,
                cfg.smtp_host,
                cfg.smtp_port,
            )
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context, timeout=15) as server:
                server.login(cfg.smtp_username, cfg.smtp_password)
                server.sendmail(sender, [to], msg.as_string())
            self._last_email_sent_at[cache_key] = now
            logger.info("email notification sent to %s subject=%r", to, subject)
        except Exception as exc:
            logger.warning("failed to send email notification to %s: %s", to, exc)
