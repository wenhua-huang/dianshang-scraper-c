"""Native JD (JINGDONG) scraper orchestrator."""
from __future__ import annotations

import logging
from typing import Any, Callable

from scraper_client.core.settings import Settings
from scraper_client.domain.models import ScrapeConfig, ShopAccountInfo
from scraper_client.infra.jd.authenticator import JDAuthenticator
from scraper_client.infra.jd.detail_extractor import JDDetailExtractor
from scraper_client.infra.jd.exceptions import JDParseError, JDScraperError, JDSessionExpiredError
from scraper_client.infra.jd.order_list_extractor import JDOrderListExtractor
from scraper_client.infra.jd.response_parser import parse_item, parse_order, parse_price_info
from scraper_client.infra.jd.session_manager import JDSessionManager

logger = logging.getLogger(__name__)


class JDScraper:
    """Coordinates JD order extraction for a single shop account."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session_manager = JDSessionManager(settings)
        self._authenticator = JDAuthenticator(self._session_manager)
        self._list_extractor = JDOrderListExtractor()
        self._detail_extractor = JDDetailExtractor()

    def scrape_config_from(self, config: ScrapeConfig | dict[str, Any] | None) -> dict[str, int]:
        if config is None:
            cfg: dict[str, Any] = {}
        elif isinstance(config, dict):
            cfg = config
        else:
            cfg = {
                "human_action_min_ms": config.human_action_min_ms,
                "human_action_max_ms": config.human_action_max_ms,
                "scrape_max_pages": config.max_pages,
            }

        min_ms = int(cfg.get("human_action_min_ms", 1000))
        max_ms = int(cfg.get("human_action_max_ms", max(min_ms, 5000)))
        pages = int(cfg.get("scrape_max_pages", cfg.get("max_pages", 10)))
        upload_batch_size = int(cfg.get("upload_batch_size", cfg.get("stream_upload_batch_size", 20)))

        if max_ms < min_ms:
            max_ms = min_ms
        return {
            "human_action_min_ms": max(0, min_ms),
            "human_action_max_ms": max(0, max_ms),
            "scrape_max_pages": max(1, pages),
            "upload_batch_size": max(1, upload_batch_size),
        }

    def scrape(
        self,
        account: ShopAccountInfo | dict[str, Any],
        config: ScrapeConfig | dict[str, Any] | None = None,
        on_batch_ready: Callable[[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]], None]
        | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Scrape orders for account and return backend-compatible payloads."""
        account_id = account.get("id") if isinstance(account, dict) else account.id
        account_name = (
            account.get("account_name") if isinstance(account, dict) else account.account_name
        )
        runtime_cfg = self.scrape_config_from(config)
        logger.info(
            "JD抓取开始 account_id=%s account_name=%s max_pages=%s upload_batch_size=%s",
            account_id,
            account_name,
            runtime_cfg["scrape_max_pages"],
            runtime_cfg["upload_batch_size"],
        )

        account_payload = account if isinstance(account, dict) else account.__dict__
        session = self._session_manager.open_session(int(account_id))
        try:
            self._authenticator.ensure_authenticated(
                session,
                account=account_payload,
                allow_manual_login=True,
                wait_timeout_seconds=max(90, runtime_cfg["human_action_max_ms"] // 10),
            )

            if on_batch_ready is None:
                raw_orders = self._extract_with_reauth_retry(
                    session=session,
                    account=account_payload,
                    runtime_cfg=runtime_cfg,
                )
                enriched_orders = self._detail_extractor.enrich(raw_orders)
                orders, items, price_info = self._parse_raw_orders(enriched_orders)
            else:
                orders, items, price_info = self._scrape_and_upload_in_batches(
                    session=session,
                    account=account_payload,
                    runtime_cfg=runtime_cfg,
                    on_batch_ready=on_batch_ready,
                )

            if orders and not items:
                raise JDParseError("item parsing produced empty payload for non-empty orders")
            if orders and not price_info:
                raise JDParseError("price parsing produced empty payload for non-empty orders")

            logger.info(
                "JD抓取完成 account_id=%s orders=%s items=%s price_info=%s",
                account_id,
                len(orders),
                len(items),
                len(price_info),
            )
            return orders, items, price_info
        except Exception as exc:
            raise JDScraperError(str(exc)) from exc
        finally:
            self._session_manager.close_session(session, persist_state=True)

    def _scrape_and_upload_in_batches(
        self,
        *,
        session,
        account: dict[str, Any],
        runtime_cfg: dict[str, int],
        on_batch_ready: Callable[[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]], None],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        batch_size = runtime_cfg["upload_batch_size"]
        seen_order_ids: set[str] = set()
        pending_raw_orders: list[dict[str, Any]] = []
        all_orders: list[dict[str, Any]] = []
        all_items: list[dict[str, Any]] = []
        all_price_info: list[dict[str, Any]] = []
        flushed_batch_count = 0

        def _flush_pending_orders(*, force: bool = False) -> None:
            nonlocal flushed_batch_count
            while len(pending_raw_orders) >= batch_size or (force and pending_raw_orders):
                current_batch_size = batch_size if len(pending_raw_orders) >= batch_size else len(pending_raw_orders)
                raw_batch = pending_raw_orders[:current_batch_size]
                del pending_raw_orders[:current_batch_size]
                enriched_orders = self._detail_extractor.enrich(raw_batch)
                orders, items, price_info = self._parse_raw_orders(enriched_orders)
                if orders and not items:
                    raise JDParseError("item parsing produced empty payload for non-empty orders")
                if orders and not price_info:
                    raise JDParseError("price parsing produced empty payload for non-empty orders")

                all_orders.extend(orders)
                all_items.extend(items)
                all_price_info.extend(price_info)
                flushed_batch_count += 1
                logger.info(
                    "准备上传批次 batch_num=%s batch_orders=%s pending_orders=%s account_id=%s",
                    flushed_batch_count,
                    len(orders),
                    len(pending_raw_orders),
                    account.get("id"),
                )
                on_batch_ready(orders, items, price_info)

        def _on_page_orders(page_orders: list[dict[str, Any]]) -> None:
            unique_orders: list[dict[str, Any]] = []
            for raw_order in page_orders:
                order_key = str(
                    raw_order.get("orderId")
                    or raw_order.get("orderNo")
                    or raw_order.get("order_no")
                    or raw_order.get("outer_order_id")
                    or ""
                )
                if not order_key or order_key in seen_order_ids:
                    continue
                seen_order_ids.add(order_key)
                unique_orders.append(raw_order)

            if not unique_orders:
                return

            pending_raw_orders.extend(unique_orders)
            logger.info(
                "加入队列（本页去重后）page_orders=%s pending_orders=%s account_id=%s",
                len(unique_orders),
                len(pending_raw_orders),
                account.get("id"),
            )
            _flush_pending_orders()

        raw_orders = self._extract_with_reauth_retry(
                session=session,
            account=account,
            runtime_cfg=runtime_cfg,
            on_page_orders=_on_page_orders,
        )

        missing_orders = []
        for raw_order in raw_orders:
            order_key = str(
                raw_order.get("orderId")
                or raw_order.get("orderNo")
                or raw_order.get("order_no")
                or raw_order.get("outer_order_id")
                or ""
            )
            if not order_key or order_key in seen_order_ids:
                continue
            seen_order_ids.add(order_key)
            missing_orders.append(raw_order)

        if missing_orders:
            pending_raw_orders.extend(missing_orders)
            logger.info(
                "补充漏网订单至队列 orders=%s pending_orders=%s account_id=%s",
                len(missing_orders),
                len(pending_raw_orders),
                account.get("id"),
            )

        _flush_pending_orders(force=True)
        return all_orders, all_items, all_price_info

    def _extract_with_reauth_retry(
        self,
        *,
        session,
        account: dict[str, Any],
        runtime_cfg: dict[str, int],
        on_page_orders: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return self._list_extractor.extract(
                session.page,
                max_pages=runtime_cfg["scrape_max_pages"],
                human_action_min_ms=runtime_cfg["human_action_min_ms"],
                human_action_max_ms=runtime_cfg["human_action_max_ms"],
                on_page_orders=on_page_orders,
            )
        except JDSessionExpiredError:
            logger.warning(
                "JD会话在抓取中过期，本轮重新登录 account_id=%s",
                account.get("id"),
            )
            self._authenticator.ensure_authenticated(
                session,
                account=account,
                allow_manual_login=True,
                wait_timeout_seconds=max(90, runtime_cfg["human_action_max_ms"] // 10),
            )
            logger.info(
                "重新登录成功，重试订单列表抓取 account_id=%s",
                account.get("id"),
            )
            return self._list_extractor.extract(
                session.page,
                max_pages=runtime_cfg["scrape_max_pages"],
                human_action_min_ms=runtime_cfg["human_action_min_ms"],
                human_action_max_ms=runtime_cfg["human_action_max_ms"],
                on_page_orders=on_page_orders,
            )

    def _parse_raw_orders(
        self, raw_orders: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Normalise a list of raw JD order dicts into backend records."""
        orders: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        price_info: list[dict[str, Any]] = []

        for raw in raw_orders:
            try:
                order = parse_order(raw)
                orders.append(order)
                oid = order["outer_order_id"]

                for raw_item in (
                    raw.get("orderItems")      # new sff API format
                    or raw.get("skuInfos")
                    or raw.get("items")
                    or []
                ):
                    items.append(parse_item(oid, raw_item))

                raw_price = raw.get("priceInfo") or raw.get("price_info") or raw
                price_info.append(parse_price_info(oid, raw_price))
            except Exception:
                logger.exception("解析原始订单数据失败 order_id=%r", raw.get("orderId"))

        return orders, items, price_info
