from __future__ import annotations

import logging
import random
from typing import Any

from playwright.sync_api import Page, Response

logger = logging.getLogger(__name__)

# 全量订单列表 URL（SPA 会跳转到 /jdm/trade/orders/order-list?tabType=allOrders）
_ORDER_LIST_ALL_URL = "https://shop.jd.com/jdm/trade/order/orderList?tabType=allOrders"

# sff.jd.com 订单列表分页 API 关键词
_ORDER_API_FRAGMENT = "orderListBffService.queryOrderPage"


class JDOrderListExtractor:
    """Extract raw order list from JD merchant backend via SFF API interception."""

    def extract(
        self,
        page: Page,
        *,
        max_pages: int,
        human_action_min_ms: int,
        human_action_max_ms: int,
    ) -> list[dict[str, Any]]:
        captured_pages: list[list[dict[str, Any]]] = []
        total_item: int | None = None
        page_size: int = 10

        def _on_response(response: Response) -> None:
            nonlocal total_item, page_size
            if _ORDER_API_FRAGMENT not in response.url:
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and "application" not in content_type:
                return
            try:
                payload = response.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            code = payload.get("code")
            if code not in (0, 200, "0", "200", None):
                return
            data = payload.get("data", {})
            if not isinstance(data, dict):
                return
            results = data.get("results", [])
            if not isinstance(results, list) or not results:
                return
            if total_item is None:
                total_item = int(data.get("totalItem", 0))
                page_size = max(1, int(data.get("pageSize", 10)))
            captured_pages.append(results)
            logger.debug(
                "captured order page results=%s totalItem=%s", len(results), total_item
            )

        page.on("response", _on_response)

        page.goto(_ORDER_LIST_ALL_URL, wait_until="domcontentloaded", timeout=90_000)
        # 等待微前端 SPA 渲染并触发首页 API 调用
        page.wait_for_timeout(8_000)

        max_pages = max(1, int(max_pages))
        pages_fetched = 1

        while pages_fetched < max_pages:
            if total_item is not None and (pages_fetched * page_size) >= total_item:
                break
            if not self._goto_next_page(page):
                break
            sleep_ms = random.randint(
                max(0, human_action_min_ms),
                max(human_action_min_ms, human_action_max_ms),
            )
            page.wait_for_timeout(sleep_ms + 3_000)  # extra wait for API response
            pages_fetched += 1

        page.remove_listener("response", _on_response)

        all_orders: list[dict[str, Any]] = []
        for batch in captured_pages:
            all_orders.extend(batch)

        # De-duplicate by orderId
        deduped: dict[str, dict[str, Any]] = {}
        for raw in all_orders:
            order_id = str(raw.get("orderId") or "")
            if order_id:
                deduped[order_id] = raw

        orders = list(deduped.values())
        logger.info(
            "order list extracted orders=%s (total_item=%s pages=%s)",
            len(orders),
            total_item,
            pages_fetched,
        )
        return orders

    def _goto_next_page(self, page: Page) -> bool:
        """Click the next-page button in the JD merchant order list."""
        candidates = [
            # JD jdm order list pagination
            "button.jd-pagination__button-next:not([disabled])",
            ".jd-pagination__button-next:not(.is-disabled)",
            "li.jd-pager-next:not(.disabled) a",
            "button[aria-label='下一页']:not([disabled])",
            "a.next:not(.disabled)",
            ".pagination-next:not(.disabled)",
        ]
        for selector in candidates:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=5_000)
                return True
            except Exception:
                continue
        return False

        walk(payload)
        return results
