"""Native JD (JINGDONG) scraper orchestrator."""
from __future__ import annotations

import logging
from typing import Any

from scraper_client.core.settings import Settings
from scraper_client.domain.models import ScrapeConfig, ShopAccountInfo
from scraper_client.infra.jd.authenticator import JDAuthenticator
from scraper_client.infra.jd.detail_extractor import JDDetailExtractor
from scraper_client.infra.jd.exceptions import JDParseError, JDScraperError
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

        if max_ms < min_ms:
            max_ms = min_ms
        return {
            "human_action_min_ms": max(0, min_ms),
            "human_action_max_ms": max(0, max_ms),
            "scrape_max_pages": max(1, pages),
        }

    def scrape(
        self,
        account: ShopAccountInfo | dict[str, Any],
        config: ScrapeConfig | dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Scrape orders for account and return backend-compatible payloads."""
        account_id = account.get("id") if isinstance(account, dict) else account.id
        account_name = (
            account.get("account_name") if isinstance(account, dict) else account.account_name
        )
        runtime_cfg = self.scrape_config_from(config)
        logger.info(
            "jd scrape start account_id=%s account_name=%s max_pages=%s",
            account_id,
            account_name,
            runtime_cfg["scrape_max_pages"],
        )

        session = self._session_manager.open_session(int(account_id))
        try:
            self._authenticator.ensure_authenticated(
                session,
                account=account if isinstance(account, dict) else account.__dict__,
                allow_manual_login=True,
                wait_timeout_seconds=max(90, runtime_cfg["human_action_max_ms"] // 10),
            )

            raw_orders = self._list_extractor.extract(
                session.page,
                max_pages=runtime_cfg["scrape_max_pages"],
                human_action_min_ms=runtime_cfg["human_action_min_ms"],
                human_action_max_ms=runtime_cfg["human_action_max_ms"],
            )
            enriched_orders = self._detail_extractor.enrich(raw_orders)
            orders, items, price_info = self._parse_raw_orders(enriched_orders)

            if orders and not items:
                raise JDParseError("item parsing produced empty payload for non-empty orders")
            if orders and not price_info:
                raise JDParseError("price parsing produced empty payload for non-empty orders")

            logger.info(
                "jd scrape done account_id=%s orders=%s items=%s price_info=%s",
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
                logger.exception("failed to parse raw order: %r", raw.get("orderId"))

        return orders, items, price_info
