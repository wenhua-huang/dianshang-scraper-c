from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Response

from scraper_client.infra.jd.exceptions import JDSessionExpiredError

logger = logging.getLogger(__name__)

# 全量订单列表 URL（SPA 会跳转到 /jdm/trade/orders/order-list?tabType=allOrders）
_ORDER_LIST_ALL_URL = "https://shop.jd.com/jdm/trade/orders/order-list?tabType=allOrders"

# sff.jd.com 订单列表分页 API 关键词
_ORDER_API_FRAGMENTS = [
    "orderListBffService.queryOrderPage",  # old endpoint name
    "order-list",                          # new micro-frontend route style
    "orderList",                           # camelCase variant
    "queryOrderPage",                      # core action name
]


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
        response_page_count = 0

        def _on_response(response: Response) -> None:
            nonlocal total_item, page_size, response_page_count
            if not self._is_order_response_url(response.url):
                return

            try:
                payload = response.json()
            except Exception:
                return
            data = self._extract_order_data(payload)
            if data is None:
                return

            results = data.get("results", [])
            if not isinstance(results, list) or not results:
                return
            if total_item is None:
                total_item = int(data.get("totalItem", 0))
                page_size = max(1, int(data.get("pageSize", 10)))
            captured_pages.append(results)
            response_page_count += 1
            logger.info(
                "captured order api page_index=%s page_results=%s cumulative_pages=%s total_item=%s page_size=%s url=%s",
                response_page_count,
                len(results),
                len(captured_pages),
                total_item,
                page_size,
                response.url,
            )

        page.on("response", _on_response)

        logger.info("opening order list page url=%s", _ORDER_LIST_ALL_URL)
        page.goto(_ORDER_LIST_ALL_URL, wait_until="domcontentloaded", timeout=90_000)
        if self._is_login_redirect_or_page(page):
            raise JDSessionExpiredError("redirected to login page when opening JD order list")

        self._ensure_all_orders_tab(page)
        # 等待微前端 SPA 渲染并触发首页 API 调用
        page.wait_for_timeout(8_000)
        if self._is_login_redirect_or_page(page):
            raise JDSessionExpiredError("session expired before JD order list became ready")

        logger.info("order list first page load wait finished")

        max_pages = max(1, int(max_pages))
        pages_fetched = 1

        while pages_fetched < max_pages:
            if total_item is not None and (pages_fetched * page_size) >= total_item:
                logger.info(
                    "stop pagination reached total bound pages_fetched=%s page_size=%s total_item=%s",
                    pages_fetched,
                    page_size,
                    total_item,
                )
                break
            logger.info("turning page current_page=%s target_page=%s", pages_fetched, pages_fetched + 1)
            if not self._goto_next_page(page):
                logger.info("stop pagination next-page control not found current_page=%s", pages_fetched)
                break
            sleep_ms = random.randint(
                max(0, human_action_min_ms),
                max(human_action_min_ms, human_action_max_ms),
            )
            page.wait_for_timeout(sleep_ms + 3_000)  # extra wait for API response
            pages_fetched += 1
            logger.info("page turn done current_page=%s wait_ms=%s", pages_fetched, sleep_ms + 3_000)

        page.remove_listener("response", _on_response)

        all_orders: list[dict[str, Any]] = []
        for batch in captured_pages:
            all_orders.extend(batch)

        orders = self._dedupe_orders(all_orders)
        logger.info(
            "order list extracted orders=%s total_item=%s pages_clicked=%s response_pages=%s",
            len(orders),
            total_item,
            pages_fetched,
            response_page_count,
        )

        if response_page_count == 0:
            logger.warning(
                "no order API response captured, enabling DOM fallback max_pages=%s",
                max_pages,
            )
            dom_orders, dom_pages = self._extract_orders_by_dom(
                page,
                max_pages=max_pages,
                human_action_min_ms=human_action_min_ms,
                human_action_max_ms=human_action_max_ms,
            )
            logger.info(
                "dom fallback finished orders=%s pages=%s",
                len(dom_orders),
                dom_pages,
            )
            if dom_orders:
                return dom_orders

            self._log_zero_result_diagnostics(page)
            if self._is_login_redirect_or_page(page):
                raise JDSessionExpiredError("session expired during JD order list extraction")

        return orders

    def _goto_next_page(self, page: Page) -> bool:
        """Click the next-page button in the JD merchant order list."""
        candidates = [
            # JD jdm order list pagination
            "button.jd-pagination__button-next:not([disabled])",
            ".jd-pagination__button-next:not(.is-disabled)",
            ".jd-pagination .btn-next",
            ".jd-pro-pagination .btn-next",
            "li.jd-pager-next:not(.disabled) a",
            "button[aria-label='下一页']:not([disabled])",
            "button:has-text('下一页')",
            "a:has-text('下一页')",
            "a.next:not(.disabled)",
            ".pagination-next:not(.disabled)",
        ]
        targets = [page, *[frame for frame in page.frames if frame != page.main_frame]]
        for target_idx, target in enumerate(targets):
            for selector in candidates:
                try:
                    locator = target.locator(selector)
                    if locator.count() == 0:
                        continue
                    for idx in range(locator.count()):
                        candidate = locator.nth(idx)
                        if not candidate.is_visible() or not candidate.is_enabled():
                            continue
                        class_name = (candidate.get_attribute("class") or "").lower()
                        aria_disabled = (candidate.get_attribute("aria-disabled") or "").lower()
                        if "disabled" in class_name or aria_disabled == "true":
                            continue
                        candidate.click(timeout=5_000)
                        logger.info(
                            "clicked next page selector=%s index=%s target=%s",
                            selector,
                            idx,
                            "page" if target_idx == 0 else f"frame-{target_idx}",
                        )
                        return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _is_order_response_url(url: str) -> bool:
        lower_url = (url or "").lower()
        if "shop.jd.com" not in lower_url and "sff.jd.com" not in lower_url:
            return False
        return any(fragment.lower() in lower_url for fragment in _ORDER_API_FRAGMENTS)

    @staticmethod
    def _extract_order_data(payload: Any) -> dict[str, Any] | None:
        """Return the first payload block that looks like order-page data."""
        if not isinstance(payload, dict):
            return None

        code = payload.get("code")
        if code not in (0, 200, "0", "200", None):
            return None

        candidates: list[Any] = [
            payload.get("data"),
            payload.get("result"),
        ]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend([
                data.get("data"),
                data.get("result"),
                data.get("page"),
                data.get("pageData"),
            ])

        for item in candidates:
            if not isinstance(item, dict):
                continue
            results = item.get("results") or item.get("list") or item.get("records")
            if isinstance(results, list):
                normalized = dict(item)
                normalized["results"] = results
                return normalized
        return None

    @staticmethod
    def _dedupe_orders(raw_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for raw in raw_orders:
            order_key = str(
                raw.get("orderId")
                or raw.get("orderNo")
                or raw.get("order_no")
                or raw.get("outer_order_id")
                or ""
            )
            if not order_key:
                continue
            deduped[order_key] = raw
        return list(deduped.values())

    def _extract_orders_by_dom(
        self,
        page: Page,
        *,
        max_pages: int,
        human_action_min_ms: int,
        human_action_max_ms: int,
    ) -> tuple[list[dict[str, Any]], int]:
        page.goto(_ORDER_LIST_ALL_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(6_000)

        max_pages = max(1, int(max_pages))
        page_index = 1
        all_orders: list[dict[str, Any]] = []

        while page_index <= max_pages:
            page_orders = self._read_order_cards_from_dom(page)
            logger.info(
                "dom fallback page extracted page=%s orders=%s",
                page_index,
                len(page_orders),
            )
            all_orders.extend(page_orders)

            if page_index >= max_pages:
                break
            if not self._goto_next_page(page):
                logger.info("dom fallback stop: next-page control not found page=%s", page_index)
                break

            sleep_ms = random.randint(
                max(0, human_action_min_ms),
                max(human_action_min_ms, human_action_max_ms),
            )
            page.wait_for_timeout(sleep_ms + 2_000)
            page_index += 1

        return self._dedupe_orders(all_orders), page_index

    @staticmethod
    def _read_order_cards_from_dom(page: Page) -> list[dict[str, Any]]:
        targets = [page, *[frame for frame in page.frames if frame != page.main_frame]]
        all_rows: list[dict[str, Any]] = []
        for target_idx, target in enumerate(targets):
            try:
                rows = target.evaluate(
                    r"""
                    () => {
                      const cards = Array.from(document.querySelectorAll('.order-list-card-table .table-body .card'));
                      const results = [];

                      for (const card of cards) {
                        const cardText = (card.innerText || '').trim();
                        if (!cardText) continue;

                        const idNode = card.querySelector('.shop-order-id button span');
                        let orderNo = idNode ? (idNode.textContent || '').trim() : '';
                        if (!orderNo) {
                          const m = cardText.match(/(?:订单号|订单编号)\s*[:：]?\s*([A-Za-z0-9\-]{8,})/);
                          orderNo = m ? m[1].trim() : '';
                        }
                        if (!orderNo) continue;

                        const timeNode = card.querySelector('.order-time-info');
                        const timeText = timeNode ? (timeNode.textContent || '').trim() : '';
                        const statusNode = card.querySelector('.table-cared-order-status-column .order-status');
                        const statusText = statusNode ? (statusNode.textContent || '').trim() : '';
                        const skuNode = card.querySelector('.order-sku-info-list-card, [class*="sku-info"]');
                        const itemSummary = skuNode ? (skuNode.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 400) : '';

                        const amountMatch = cardText.match(/[¥￥]\s*([\d\.]+)/);
                        const amountText = amountMatch ? amountMatch[1] : '';

                        const row = {
                          orderId: orderNo,
                          orderNo: orderNo,
                          orderStatus: statusText || 'UNKNOWN',
                          orderCreateTime: timeText,
                          item_summary: itemSummary,
                        };

                        if (amountText) {
                          row.orderTotalPrice = amountText;
                          row.orderPaymentInfo = { receivables: amountText };
                        }

                        results.push(row);
                      }
                      return results;
                    }
                    """
                )
                if isinstance(rows, list):
                    dict_rows = [item for item in rows if isinstance(item, dict)]
                    if dict_rows:
                        logger.info(
                            "dom fallback extracted from %s orders=%s",
                            "page" if target_idx == 0 else f"frame-{target_idx}",
                            len(dict_rows),
                        )
                    all_rows.extend(dict_rows)
            except Exception:
                continue
        return all_rows

    @staticmethod
    def _ensure_all_orders_tab(page: Page) -> None:
        selectors = [
            "a[href*='tabType=allOrders']",
            "button:has-text('全部订单')",
            "a:has-text('全部订单')",
            "div:has-text('全部订单')",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() == 0:
                    continue
                locator.first.click(timeout=2000)
                logger.info("ensured all-orders tab by selector=%s", selector)
                page.wait_for_timeout(1000)
                return
            except Exception:
                continue

    @staticmethod
    def _log_zero_result_diagnostics(page: Page) -> None:
        ts = int(time.time())
        debug_dir = Path("artifacts") / "debug" / "jd_zero_result"
        debug_dir.mkdir(parents=True, exist_ok=True)

        try:
            page_diag = page.evaluate(
                r"""
                () => {
                  const body = (document.body && document.body.innerText) ? document.body.innerText : '';
                  return {
                    title: document.title || '',
                    url: location.href || '',
                    iframeCount: document.querySelectorAll('iframe').length,
                    cardCount: document.querySelectorAll('.order-list-card-table .table-body .card').length,
                    hasLoginText: body.includes('登录') || body.includes('去登录'),
                    hasEmptyText: body.includes('暂无数据') || body.includes('暂无订单') || body.includes('无数据'),
                    hasNoPermissionText: body.includes('无权限') || body.includes('权限') || body.includes('未开通'),
                  };
                }
                """
            )
            logger.warning("zero-result diagnostics page=%s", page_diag)
        except Exception:
            logger.warning("zero-result diagnostics failed on main page", exc_info=True)

        try:
            screenshot_path = debug_dir / f"{ts}_main.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            logger.warning("zero-result screenshot saved path=%s", screenshot_path)
        except Exception:
            logger.warning("failed to save zero-result screenshot", exc_info=True)

        try:
            html_path = debug_dir / f"{ts}_main.html"
            html_path.write_text(page.content(), encoding="utf-8")
            logger.warning("zero-result html saved path=%s", html_path)
        except Exception:
            logger.warning("failed to save zero-result html", exc_info=True)

        for idx, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            try:
                frame_diag = frame.evaluate(
                    r"""
                    () => {
                      const body = (document.body && document.body.innerText) ? document.body.innerText : '';
                      return {
                        title: document.title || '',
                        url: location.href || '',
                        cardCount: document.querySelectorAll('.order-list-card-table .table-body .card').length,
                        hasLoginText: body.includes('登录') || body.includes('去登录'),
                        hasEmptyText: body.includes('暂无数据') || body.includes('暂无订单') || body.includes('无数据'),
                        hasNoPermissionText: body.includes('无权限') || body.includes('权限') || body.includes('未开通'),
                      };
                    }
                    """
                )
                logger.warning("zero-result diagnostics frame-%s=%s", idx, frame_diag)
            except Exception:
                continue

    @staticmethod
    def _is_login_redirect_or_page(page: Page) -> bool:
        url = (page.url or "").lower()
        if "passport.shop.jd.com" in url or "passport.jd.com" in url or "/login/" in url:
            return True
        try:
            body = page.inner_text("body", timeout=3000)
            markers = ["密码登录", "短信登录", "立即登录", "账号登录", "去登录"]
            return any(mark in body for mark in markers)
        except Exception:
            return False
