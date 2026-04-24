from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from scraper_client.core.settings import Settings

logger = logging.getLogger(__name__)


class UnsupportedPlatformError(RuntimeError):
    """Raised when an account platform is not implemented by scraper-c."""


def _amount_to_cent(value: Decimal | None, raw_text: str | None) -> int:
    if value is not None:
        return int((value * 100).quantize(Decimal("1")))
    if not raw_text:
        return 0
    cleaned = raw_text.replace("¥", "").replace(",", "").strip()
    if not cleaned:
        return 0
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal("1")))
    except Exception:
        return 0


class ScrapeExecutor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def execute_account(
        self,
        account: dict[str, Any],
        platform: str,
        scrape_config: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if platform != "JINGDONG":
            raise UnsupportedPlatformError(f"unsupported platform: {platform}")

        return self._execute_jd(account=account, scrape_config=scrape_config)

    def _execute_jd(
        self,
        account: dict[str, Any],
        scrape_config: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        # Reuse mature JD extraction from old project without modifying old repo.
        try:
            from jd_scraper.core.exceptions import SessionInvalidError  # type: ignore
            from jd_scraper.core.settings import get_settings as get_jd_settings  # type: ignore
            from jd_scraper.infra.browser.session_manager import BrowserSessionManager  # type: ignore
            from jd_scraper.services.order_scrape_service import OrderScrapeService  # type: ignore
            from jd_scraper.services.order_detail_scrape_service import (  # type: ignore
                OrderDetailScrapeService,
            )
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "jd_scraper runtime dependency missing. "
                "Please ensure jd-scraper is on PYTHONPATH and install scraper-client deps with: "
                "python -m pip install -e ."
            ) from exc

        class FastInteractiveLoginBrowserSessionManager(BrowserSessionManager):
            def _wait_for_login_completion(
                self,
                page: Any,
                *,
                login_markers: list[str] | None,
                timeout_ms: int,
            ) -> bool:
                timeout_ms = max(0, timeout_ms)
                poll_ms = min(2_000, max(250, timeout_ms // 2 or 250))
                deadline = time.time() + (timeout_ms / 1000)
                while time.time() < deadline:
                    self._handle_behavior_captcha_if_present(page)
                    if not self._is_login_required(page, login_markers):
                        logger.info("interactive login completed early current_url=%s", page.url)
                        return True
                    page.wait_for_timeout(poll_ms)
                return not self._is_login_required(page, login_markers)

            def ensure_authenticated(
                self,
                page: Any,
                allow_interactive_login: bool,
                *,
                order_list_url: str,
                base_url: str,
                login_entry_url: str | None = None,
                login_markers: list[str] | None = None,
                storage_state_path: str | None = None,
                verification_handler: Any = None,
            ) -> None:
                entry_url = login_entry_url or base_url

                self._goto_with_fallback(page, primary_url=entry_url, fallback_url=base_url)
                needs_login = self._is_login_required(page, login_markers)
                self._log_auth_judgement(page, stage="entry", needs_login=needs_login)
                self._emit_login_result(page, stage="entry", needs_login=needs_login)
                self._handle_behavior_captcha_if_present(page)

                if needs_login:
                    if allow_interactive_login:
                        logger.info("Login required. Please complete login in opened browser window.")
                        self._goto_with_fallback(page, primary_url=entry_url, fallback_url=base_url)
                        low_ms = max(0, int(self.settings.human_action_min_ms))
                        high_ms = max(low_ms, int(self.settings.human_action_max_ms))
                        wait_ms = random.randint(low_ms, high_ms) if high_ms > low_ms else high_ms
                        completed = self._wait_for_login_completion(
                            page,
                            login_markers=login_markers,
                            timeout_ms=wait_ms,
                        )
                        if not completed:
                            logger.warning(
                                "interactive login did not complete within waiting window wait_ms=%s",
                                wait_ms,
                            )
                        if not self.settings.playwright_connect_over_cdp:
                            self._save_storage_state(page.context, storage_state_path)
                    elif verification_handler:
                        self._goto_with_fallback(page, primary_url=entry_url, fallback_url=base_url)
                        verified = verification_handler(page)
                        self._handle_behavior_captcha_if_present(page)
                        needs_login_after_verify = self._is_login_required(page, login_markers)
                        if verified or not needs_login_after_verify:
                            if not self.settings.playwright_connect_over_cdp:
                                self._save_storage_state(page.context, storage_state_path)
                        else:
                            raise SessionInvalidError("Verification handler failed to complete login.")
                    else:
                        raise SessionInvalidError(
                            "Session is invalid. Re-run with --interactive-login to login manually."
                        )

                page.goto(order_list_url)
                needs_login = self._is_login_required(page, login_markers)
                self._log_auth_judgement(page, stage="order-list", needs_login=needs_login)
                self._emit_login_result(page, stage="order-list", needs_login=needs_login)
                self._handle_behavior_captcha_if_present(page)
                if needs_login:
                    raise SessionInvalidError("Login did not complete successfully.")

        # Bridge settings from new client env to old extractor settings at runtime.
        jd_settings = get_jd_settings()
        jd_settings.playwright_connect_over_cdp = True
        jd_settings.playwright_cdp_url = self.settings.playwright_cdp_url
        jd_settings.playwright_timeout_ms = self.settings.playwright_timeout_ms
        # Apply server-delivered behavioral params
        if scrape_config:
            if "human_action_min_ms" in scrape_config:
                jd_settings.human_action_min_ms = int(scrape_config["human_action_min_ms"])
            if "human_action_max_ms" in scrape_config:
                jd_settings.human_action_max_ms = int(scrape_config["human_action_max_ms"])
            if "max_pages" in scrape_config:
                jd_settings.scrape_max_pages = int(scrape_config["max_pages"])
        if account.get("email"):
            jd_settings.smtp_to = str(account.get("email"))
            # If SMTP sender credentials are configured, enable captcha email notifications.
            if jd_settings.smtp_host and jd_settings.smtp_username and jd_settings.smtp_password:
                jd_settings.smtp_enabled = True

        session_manager = FastInteractiveLoginBrowserSessionManager(jd_settings)
        order_scraper = OrderScrapeService(jd_settings, session_manager)
        detail_scraper = OrderDetailScrapeService(jd_settings, session_manager)

        shop_id = str(account["shop_id"])

        # Keep account-level storage state path stable to avoid repeated login.
        storage_state_path = f"artifacts/storage/jd_account_{account['id']}.json"

        try:
            # Standard flow: auto fill account/password + verification code handling.
            orders = order_scraper.scrape_orders(
                allow_interactive_login=False,
                shop_id=shop_id,
                storage_state_path=storage_state_path,
                verification_handler=None,
            )
        except SessionInvalidError:
            # Fallback flow: when still not logged in, switch to interactive login and continue.
            logger.warning(
                "auto login failed for account_id=%s, fallback to interactive login",
                account["id"],
            )
            orders = order_scraper.scrape_orders(
                allow_interactive_login=True,
                shop_id=shop_id,
                storage_state_path=storage_state_path,
                verification_handler=None,
            )
        details, skus = detail_scraper.scrape_details(
            orders,
            storage_state_path=storage_state_path,
        )

        now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")

        orders_payload: list[dict[str, Any]] = []
        for order in orders:
            created_time = order.order_time.replace(tzinfo=None) if order.order_time else datetime.now(UTC).replace(tzinfo=None)
            orders_payload.append(
                {
                    "outer_order_id": order.external_order_no,
                    "status": str(order.status),
                    "platform_create_time": created_time.isoformat(timespec="seconds"),
                    "platform_update_time": now_iso,
                    "total_amount": _amount_to_cent(order.total_amount_value, order.total_amount),
                    "raw_data": {
                        "buyer_masked": order.buyer_masked,
                        "item_summary": order.item_summary,
                        "source": "dianshang-scraper-c",
                        "raw_payload": order.raw_payload,
                    },
                }
            )

        detail_map = {d.external_order_no: d for d in details}
        items_payload: list[dict[str, Any]] = []
        for sku in skus:
            detail = detail_map.get(sku.external_order_no)
            items_payload.append(
                {
                    "outer_order_id": sku.external_order_no,
                    "product_id": sku.product_id or "UNKNOWN",
                    "product_name": sku.product_name or "",
                    "sku_id": sku.sku_id,
                    "sku_name": sku.sku_key,
                    "quantity": int(sku.quantity or 1),
                    "unit_price": _amount_to_cent(sku.unit_price_value, sku.unit_price),
                    "total_price": _amount_to_cent(None, sku.total_price),
                    "raw_data": {
                        "sku_key": sku.sku_key,
                        "sku_link_id": sku.sku_link_id,
                        "image_url": sku.image_url,
                        "sku_images": sku.sku_images,
                        "promotion": sku.promotion,
                        "detail_tracking_no": detail.tracking_no if detail else None,
                        "detail_logistics_events": detail.logistics_events if detail else [],
                    },
                }
            )

        price_payload: list[dict[str, Any]] = []
        for order in orders:
            price_payload.append(
                {
                    "outer_order_id": order.external_order_no,
                    "order_price": _amount_to_cent(order.total_amount_value, order.total_amount),
                    "raw_data": {
                        "source": "order_total_amount",
                    },
                }
            )

        logger.info(
            "scrape done account_id=%s orders=%s details=%s skus=%s",
            account["id"],
            len(orders),
            len(details),
            len(skus),
        )
        return orders_payload, items_payload, price_payload
