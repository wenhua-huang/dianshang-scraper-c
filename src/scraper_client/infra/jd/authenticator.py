from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import urlsplit

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

# 账号密码不匹配提示特征
_CREDENTIAL_MISMATCH_TEXT = [
    "账号名与密码不匹配，请重新输入",
    "账号名与密码不匹配",
    "用户名或密码不正确",
    "账号或密码错误",
    "密码错误",
]


class JDAuthenticator:
    """Ensure JD account has a valid authenticated browser session."""

    def __init__(self, session_manager: JDSessionManager, settings: Settings | None = None) -> None:
        self._session_manager = session_manager
        self._settings = settings or get_settings()
        self._api_client = InternalApiClient(
            base_url=self._settings.scraper_server_base_url,
            api_key=self._settings.scraper_internal_api_key,
            verify_ssl=self._settings.scraper_verify_ssl,
        )
        self._last_email_sent_at: dict[str, float] = {}
        self._latest_sms_verification_session: dict[int, dict[str, Any]] = {}
        self._last_sms_flow_attempt_at: dict[int, float] = {}
        self._sms_flow_started_at: dict[int, float] = {}
        self._sms_flow_abandoned: set[int] = set()
        self._sms_flow_abandon_notified: set[int] = set()

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
            "登录检查开始 account_id=%s account_name=%s allow_manual_login=%s force_relogin=%s",
            account.get("id"),
            account.get("account_name"),
            allow_manual_login,
            force_relogin,
        )

        if force_relogin:
            try:
                session.context.clear_cookies()
                logger.info("强制重新登录：已清除浏览器 Cookie account_id=%s", account.get("id"))
            except Exception:
                logger.warning(
                    "强制重新登录：清除 Cookie 失败 account_id=%s",
                    account.get("id"),
                    exc_info=True,
                )

        page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
        logger.info("登录检查：已访问订单列表页 target=%s current_url=%s", JD_ORDER_LIST_URL, page.url)
        if not force_relogin and not self._is_login_required(page):
            if self._is_order_list_page(page):
                logger.info(
                    "登录检查通过（复用现有 Session）account_id=%s 无需输入凭证",
                    account.get("id"),
                )
                return
            logger.warning(
                "Session 状态已登录，但当前页不是订单列表 account_id=%s current_url=%s",
                account.get("id"),
                page.url,
            )

        if force_relogin and not self._is_login_required(page):
            logger.warning(
                "已请求强制重新登录，但 Session 仍显示已登录 account_id=%s；继续尝试凭证登录",
                account.get("id"),
            )

        # Retry once with persisted state reloaded from disk.
        if not force_relogin:
            logger.info(
                "登录检查首次未通过，重新加载持久化 storage_state account_id=%s",
                account.get("id"),
            )
            self._session_manager._load_storage_state(session.context, session.storage_state_path)
            page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
            if not self._is_login_required(page):
                if self._is_order_list_page(page):
                    logger.info(
                        "重加载 storage_state 后登录检查通过 account_id=%s 无需输入凭证",
                        account.get("id"),
                    )
                    return
                logger.warning(
                    "重加载 storage_state 后仍未到订单列表页 account_id=%s current_url=%s",
                    account.get("id"),
                    page.url,
                )

        # Try credential-based login before falling back to manual login.
        logger.info("仍需登录，尝试使用账号密码自动登录 account_id=%s", account.get("id"))
        if self._try_auto_login(page, account=account, wait_timeout_seconds=wait_timeout_seconds):
            self._session_manager._save_storage_state(session.context, session.storage_state_path)
            logger.info("账号密码登录成功 account_id=%s", account.get("id"))
            return

        if not allow_manual_login:
            raise JDSessionExpiredError("session expired, manual login required")

        logger.warning(
            "需要手动登录京东商家后台 account_id=%s account_name=%s —— "
            "请在已打开的浏览器窗口完成登录",
            account.get("id"),
            account.get("account_name"),
        )
        # 导航到商家后台首页让用户手动登录
        page.goto(JD_MERCHANT_LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)

        deadline = time.time() + max(30, int(wait_timeout_seconds))
        while time.time() < deadline:
            self._raise_on_credential_mismatch(page, account=account)
            if not self._is_login_required(page):
                page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
                if not self._is_login_required(page) and self._is_order_list_page(page):
                    self._session_manager._save_storage_state(session.context, session.storage_state_path)
                    logger.info("手动登录成功 account_id=%s", account.get("id"))
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
            logger.info("跳过自动登录：account_name 或 account_password 为空")
            return False

        logger.info(
            "尝试账号密码登录 account_id=%s username=%s",
            account.get("id"),
            username,
        )
        page.goto(JD_MERCHANT_LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)

        if not self._is_login_required(page):
            page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
            if not self._is_login_required(page) and self._is_order_list_page(page):
                return True

        self._switch_to_password_login(page)
        logger.info("已切换至密码登录标签页 account_id=%s", account.get("id"))

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
            logger.warning("自动登录失败：未找到用户名输入框")
            return False
        logger.info("已输入用户名 account_id=%s", account.get("id"))

        if not self._fill_first_available(page, password_selectors, password):
            logger.warning("自动登录失败：未找到密码输入框")
            return False
        logger.info(
            "已输入密码 account_id=%s password_length=%s",
            account.get("id"),
            len(password),
        )

        # Give JD login form a short settle window before submit click.
        page.wait_for_timeout(300)

        clicked = self._click_login_submit(page, submit_selectors)
        if not clicked:
            # Last resort: submit from password input.
            if not self._press_enter_first_available(page, password_selectors):
                logger.warning("自动登录失败：未找到提交按钮")
                return False
            logger.info("通过回车键提交登录 account_id=%s", account.get("id"))
        else:
            logger.info("已点击登录提交按钮 account_id=%s", account.get("id"))

        # JD may switch into SMS verification after credential submit.
        page.wait_for_timeout(500)
        self._raise_on_credential_mismatch(page, account=account)
        if self._detect_sms_verification(page):
            logger.warning("提交登录后检测到短信验证码 account_id=%s", account.get("id"))
            if not self._handle_sms_verification(page, account=account):
                logger.warning("短信验证码流程失败 account_id=%s", account.get("id"))
                return False

        deadline = time.time() + max(20, int(wait_timeout_seconds))
        while time.time() < deadline:
            page.wait_for_timeout(1500)
            self._raise_on_credential_mismatch(page, account=account)
            self._handle_verification_if_present(page, account=account)
            if not self._is_login_required(page):
                page.goto(JD_ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
                if not self._is_login_required(page) and self._is_order_list_page(page):
                    logger.info("账号密码登录成功，已确认 account_id=%s", account.get("id"))
                    return True

        logger.warning("账号密码登录超时 account_id=%s", account.get("id"))
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
            logger.warning("跳过短信验证：无效的账号 ID")
            return False

        if self._fill_sms_phone(page, account=account):
            logger.info("已输入短信手机号 account_id=%s", account_id)
        else:
            logger.info("手机号输入框未找到或已跳过 account_id=%s", account_id)

        if not self._trigger_sms_send(page):
            logger.warning("未找到发送验证码按钮 account_id=%s", account_id)
            return False
        logger.info("已点击发送验证码按钮 account_id=%s", account_id)

        try:
            session = self._api_client.create_verification_session(account_id)
        except Exception:
            logger.exception("创建验证码会话失败 account_id=%s", account_id)
            return False

        request_id = str(session.get("request_id") or "").strip()
        if not request_id:
            logger.warning("验证码会话缺少 request_id account_id=%s", account_id)
            return False

        self._latest_sms_verification_session[account_id] = session
        logger.info("验证码会话已创建 account_id=%s request_id=%s", account_id, request_id)
        # 链接邮件统一由后端 create_verification_session 发送，抓取端不再重复发送，
        # 避免账号未配置 email 时落到 smtp_to 上出现两封等价邮件。
        deadline = time.time() + int(self._settings.verification_max_wait_seconds)
        poll_seconds = float(self._settings.verification_poll_interval_seconds)

        while time.time() < deadline:
            try:
                status_payload = self._api_client.get_verification_status(request_id)
            except Exception:
                logger.exception("查询验证码状态失败 request_id=%s", request_id)
                page.wait_for_timeout(int(poll_seconds * 1000))
                continue

            status_text = str(status_payload.get("status") or "")
            code_available = bool(status_payload.get("code_available"))
            logger.info(
                "验证码轮询 account_id=%s request_id=%s status=%s code_available=%s",
                account_id,
                request_id,
                status_text,
                code_available,
            )

            if status_text == "SUBMITTED" or code_available:
                try:
                    consumed = self._api_client.consume_verification_code(request_id)
                except Exception:
                    logger.exception("消费验证码失败 request_id=%s", request_id)
                    return False

                code = str(consumed.get("code") or "").strip()
                if not code:
                    logger.warning("验证码为空 request_id=%s", request_id)
                    return False
                logger.info("已收到验证码 request_id=%s", request_id)
                return self._submit_code_to_page(page, code)

            if status_text in {"EXPIRED", "CONSUMED"}:
                logger.warning("验证码会话已结束 request_id=%s status=%s", request_id, status_text)
                return False

            page.wait_for_timeout(int(poll_seconds * 1000))

        logger.warning("等待验证码超时 request_id=%s", request_id)
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
        # 某些认证页需要先点击“使用手机短信验证码”切换认证方式。
        switch_selectors = [
            "button:has-text('使用 手机短信验证码')",
            "button:has-text('使用手机短信验证码')",
            "button:has-text('手机短信验证码')",
            "text=使用 手机短信验证码",
            "text=使用手机短信验证码",
            ".tip-btnbox button",
            "button.btn-def.btn-xl.mb20",
        ]
        switched = self._click_login_submit(page, switch_selectors)
        if switched:
            logger.info("已切换到短信验证码认证方式")
            page.wait_for_timeout(500)

        selectors = [
            "button:has-text('发送验证码')",
            "button:has-text('获取验证码')",
            "button:has-text('获取短信验证码')",
            "button:has-text('发送短信验证码')",
            "button:has-text('发送短信')",
            "button:has-text('获取短信')",
            "button:has-text('重新发送')",
            "button:has-text('重新获取')",
            "a:has-text('获取验证码')",
            "a:has-text('发送验证码')",
            "a:has-text('重新发送')",
            "span:has-text('获取验证码')",
            "span:has-text('发送验证码')",
            "span:has-text('发送短信验证码')",
            "div:has-text('获取验证码')",
            "div:has-text('发送验证码')",
            "text=获取验证码",
            "text=发送验证码",
            "text=获取短信验证码",
            "text=发送短信验证码",
            "text=重新发送",
            "[class*='get-code']",
            "[class*='getCode']",
            "[class*='send-code']",
            "[class*='sendCode']",
            "[class*='fetch-code']",
            "[class*='fetchCode']",
            "[class*='sms']",
            "[class*='verify']",
            "[id*='sendCode']",
            "[id*='getCode']",
        ]
        return self._click_login_submit(page, selectors)

    def _build_verification_link(self, session: dict[str, Any]) -> str:
        verify_token = str(session.get("verify_token") or "").strip()
        if not verify_token:
            return ""

        frontend_base_url = str(self._settings.scraper_frontend_base_url or "").strip()
        if not frontend_base_url:
            parsed = urlsplit(self._settings.scraper_server_base_url)
            if parsed.scheme and parsed.netloc:
                frontend_base_url = f"{parsed.scheme}://{parsed.netloc}"

        if not frontend_base_url:
            return ""

        return f"{frontend_base_url.rstrip('/')}/verify-code/{verify_token}"

    def _send_sms_verification_link_email(
        self,
        *,
        account: dict[str, Any],
        session: dict[str, Any],
    ) -> None:
        to = self._resolve_notification_recipient(account)
        if not to:
            return

        account_email = str(account.get("email") or "").strip()
        if account_email and to == account_email:
            return

        verify_link = self._build_verification_link(session)
        if not verify_link:
            logger.warning(
                "验证码会话缺少 verify_token，无法发送链接邮件 account_id=%s request_id=%s",
                account.get("id"),
                session.get("request_id"),
            )
            return

        self._send_email_notification(
            to=to,
            subject=f"[京东登录] 验证码填写链接 — {account.get('account_name')}",
            body=(
                f"账号 {account.get('account_name')} 已创建短信验证码会话。\n\n"
                "请点击下面的链接填写收到的正式验证码：\n"
                f"{verify_link}\n\n"
                f"请求 ID: {session.get('request_id')}\n"
                f"账号 ID: {account.get('id')}"
            ),
        )

    def _build_sms_verification_fallback_body(self, account: dict[str, Any]) -> str:
        body = (
            f"账号 {account.get('account_name')} 在登录京东商家后台时需要输入短信验证码，"
            "系统已尝试自动触发验证码会话并等待用户提交验证码。"
        )

        account_id = int(account.get("id") or 0)
        session = self._latest_sms_verification_session.get(account_id, {})
        verify_link = self._build_verification_link(session)
        request_id = str(session.get("request_id") or "").strip()

        if verify_link:
            body += f"\n\n验证码填写链接：\n{verify_link}"
            if request_id:
                body += f"\n\n请求 ID: {request_id}"
        else:
            body += "\n\n系统当前未拿到可用的验证码填写链接。"

        body += (
            "\n\n若仍未通过，请查收手机短信并在 Chrome 浏览器中填写验证码。"
            f"\n\n账号 ID: {account.get('id')}"
        )
        return body

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
        logger.info("已输入短信验证码 code_length=%s", len(code))
        if JDAuthenticator._click_first_available(page, submit_selectors):
            logger.info("已点击按钮提交验证码")
            return True
        submitted = JDAuthenticator._press_enter_first_available(page, input_selectors)
        if submitted:
            logger.info("已通过回车键提交验证码")
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
                "检测到行为验证码（滑块）account_id=%s account_name=%s",
                account.get("id"),
                account.get("account_name"),
            )
            to = self._resolve_notification_recipient(account)
            if to:
                logger.info(
                    "发送行为验证码提醒邮件 account_id=%s to=%s",
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
            flow_result = self._run_sms_flow_with_throttle(page, account=account)
            if flow_result == "handled":
                return

            if flow_result == "cooldown":
                logger.info(
                    "短信验证码等待用户填写中（冷却中）account_id=%s account_name=%s",
                    account.get("id"),
                    account.get("account_name"),
                )
                return

            if flow_result == "abandoned":
                logger.info(
                    "短信验证码流程已放弃，等待下一轮任务 account_id=%s account_name=%s",
                    account.get("id"),
                    account.get("account_name"),
                )
                return

            # flow_result in {"failed", "skip"} — 需要提醒
            logger.warning(
                "检测到需要输入短信验证码（自动流程未成功 result=%s）account_id=%s account_name=%s",
                flow_result,
                account.get("id"),
                account.get("account_name"),
            )
            to = self._resolve_notification_recipient(account)
            if to:
                logger.info(
                    "发送短信验证码提醒邮件 account_id=%s to=%s",
                    account.get("id"),
                    to,
                )
                self._send_email_notification(
                    to=to,
                    subject=f"[京东登录] 需要输入短信验证码 — {account.get('account_name')}",
                    body=self._build_sms_verification_fallback_body(account),
                )

    def _resolve_notification_recipient(self, account: dict[str, Any]) -> str:
        account_email = str(account.get("email") or "").strip()
        if account_email:
            return account_email
        return str(self._settings.smtp_to or "").strip()

    def _run_sms_flow_with_throttle(self, page: Page, *, account: dict[str, Any]) -> str:
        account_id = int(account.get("id") or 0)
        if account_id <= 0:
            return "skip"

        now = time.time()
        if account_id in self._sms_flow_abandoned:
            logger.info("短信验证码流程已放弃 account_id=%s", account_id)
            self._notify_sms_flow_abandoned(account)
            return "abandoned"

        started_at = self._sms_flow_started_at.get(account_id)
        if started_at is None:
            self._sms_flow_started_at[account_id] = now
            started_at = now

        max_duration = max(60, int(self._settings.sms_flow_max_duration_seconds))
        if now - started_at > max_duration:
            self._sms_flow_abandoned.add(account_id)
            logger.warning(
                "短信验证码流程超过最长持续时间，停止自动处理 account_id=%s max_duration_seconds=%s",
                account_id,
                max_duration,
            )
            self._notify_sms_flow_abandoned(account)
            return "abandoned"

        cooldown = max(0, int(self._settings.sms_flow_cooldown_seconds))
        last_attempt = self._last_sms_flow_attempt_at.get(account_id)
        if last_attempt is not None and now - last_attempt < cooldown:
            logger.info(
                "短信验证码流程处于冷却中 account_id=%s cooldown_seconds=%s remaining=%.1fs",
                account_id,
                cooldown,
                cooldown - (now - last_attempt),
            )
            return "cooldown"

        self._last_sms_flow_attempt_at[account_id] = now
        if self._handle_sms_verification(page, account=account):
            self._last_sms_flow_attempt_at.pop(account_id, None)
            self._latest_sms_verification_session.pop(account_id, None)
            self._sms_flow_started_at.pop(account_id, None)
            self._sms_flow_abandoned.discard(account_id)
            self._sms_flow_abandon_notified.discard(account_id)
            return "handled"

        return "failed"

    def _notify_sms_flow_abandoned(self, account: dict[str, Any]) -> None:
        account_id = int(account.get("id") or 0)
        if account_id <= 0:
            return
        if account_id in self._sms_flow_abandon_notified:
            return

        to = self._resolve_notification_recipient(account)
        if not to:
            return

        self._send_email_notification(
            to=to,
            subject=f"[京东登录] 短信验证码自动处理已放弃 — {account.get('account_name')}",
            body=(
                f"账号 {account.get('account_name')} 的短信验证码自动处理已超过 "
                f"{int(self._settings.sms_flow_max_duration_seconds)} 秒，"
                "已放弃自动处理，下一轮会继续爬取。\n\n"
                f"账号 ID: {account_id}"
            ),
        )
        self._sms_flow_abandon_notified.add(account_id)

    def _raise_on_credential_mismatch(self, page: Page, *, account: dict[str, Any]) -> None:
        if not self._detect_credential_mismatch(page):
            return

        account_id = account.get("id")
        account_name = account.get("account_name")
        logger.error(
            "检测到账号密码不匹配，终止当前任务 account_id=%s account_name=%s",
            account_id,
            account_name,
        )

        to = self._resolve_notification_recipient(account)
        if to:
            self._send_email_notification(
                to=to,
                subject=f"[京东登录] 账号密码不匹配，任务已失败 — {account_name}",
                body=(
                    f"账号 {account_name} 检测到“账号名与密码不匹配，请重新输入”，"
                    "当前任务已直接失败，请在后台核对账号与密码后重试。\n\n"
                    f"账号 ID: {account_id}"
                ),
            )

        raise JDLoginError("credential mismatch: account_name and password do not match")

    @staticmethod
    def _detect_credential_mismatch(page: Page) -> bool:
        try:
            body_text = page.inner_text("body", timeout=2000)
            if any(mark in body_text for mark in _CREDENTIAL_MISMATCH_TEXT):
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _detect_behavior_captcha(page: Page) -> bool:
        """返回 True 表示页面上存在行为验证码弹窗/组件。"""
        for selector in _BEHAVIOR_CAPTCHA_SELECTORS:
            try:
                if page.locator(selector).count() > 0:
                    logger.info("通过选择器匹配到行为验证码 selector=%s", selector)
                    return True
            except Exception:
                continue
        try:
            body_text = page.inner_text("body", timeout=2000)
            if any(mark in body_text for mark in _BEHAVIOR_CAPTCHA_TEXT):
                logger.info("通过页面文本匹配到行为验证码")
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
                    logger.info("通过选择器匹配到短信验证码 selector=%s", selector)
                    return True
            except Exception:
                continue
        try:
            body_text = page.inner_text("body", timeout=2000)
            if any(mark in body_text for mark in _SMS_VERIFICATION_TEXT):
                logger.info("通过页面文本匹配到短信验证码")
                return True
        except Exception:
            pass
        return False

    def _send_email_notification(self, *, to: str, subject: str, body: str) -> None:
        """通过 SMTP 发送邮件通知；配置不完整或发送失败时仅记录日志。"""
        cfg = self._settings
        if not (cfg.smtp_host and cfg.smtp_username and cfg.smtp_password):
            logger.info("SMTP 未配置，跳过邮件通知 to=%s", to)
            return

        now = time.time()
        cooldown = max(0, int(cfg.captcha_alert_cooldown_seconds))
        cache_key = f"{to}:{subject}"
        last_sent_at = self._last_email_sent_at.get(cache_key)
        if last_sent_at is not None and now - last_sent_at < cooldown:
            logger.info(
                "邮件通知被冷却时间抑制 to=%s subject=%r",
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
                "发送邮件通知 to=%s subject=%r smtp_host=%s smtp_port=%s",
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
            logger.info("邮件通知已发送 to=%s subject=%r", to, subject)
        except Exception as exc:
            logger.warning("发送邮件通知失败 to=%s: %s", to, exc)
