from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, Response
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from scraper_client.infra.jd.exceptions import JDSessionExpiredError

logger = logging.getLogger(__name__)

_AFTERSALE_LIST_ALL_URL = (
    "https://shop.jd.com/jdm/trade/after-sale/independent-after-sale/list?tabCode=all"
)

_AFTERSALE_API_FRAGMENTS = [
    "after-sale",
    "after_sale",
    "aftersale",
    "independent-after-sale",
    "serviceList",
    "service-list",
]

_AFTERSALE_PAGE_PATH_HINT = "/jdm/trade/after-sale/"


class JDAftersaleListExtractor:
    """Extract raw aftersale list from JD merchant backend by API interception."""

    def extract(
        self,
        page: Page,
        *,
        max_pages: int,
        human_action_min_ms: int,
        human_action_max_ms: int,
        on_page_aftersales: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[dict[str, Any]]:
        captured_pages: list[list[dict[str, Any]]] = []
        total_item: int | None = None
        page_size: int = 10
        response_page_count = 0

        def _on_response(response: Response) -> None:
            nonlocal total_item, page_size, response_page_count
            if not self._is_aftersale_response_url(response.url):
                return
            try:
                payload = response.json()
            except Exception:
                return

            data = self._extract_aftersale_data(payload)
            if data is None:
                return

            results = data.get("results", [])
            if not isinstance(results, list) or not results:
                return

            if total_item is None:
                total_item = int(data.get("totalItem", data.get("total", 0)) or 0)
                page_size = max(1, int(data.get("pageSize", data.get("size", 10)) or 10))

            captured_pages.append(results)
            response_page_count += 1
            logger.info(
                "拦截到售后列表API响应 page_index=%s page_results=%s cumulative_pages=%s total_item=%s page_size=%s url=%s",
                response_page_count,
                len(results),
                len(captured_pages),
                total_item,
                page_size,
                response.url,
            )
            if on_page_aftersales is not None:
                on_page_aftersales(results)

        page.on("response", _on_response)
        logger.info("打开售后列表页 url=%s", _AFTERSALE_LIST_ALL_URL)
        nav_start = time.monotonic()
        try:
            page.goto(_AFTERSALE_LIST_ALL_URL, wait_until="domcontentloaded", timeout=45_000)
            logger.info(
                "售后列表页导航完成 wait_until=domcontentloaded elapsed_ms=%s current_url=%s",
                int((time.monotonic() - nav_start) * 1000),
                page.url,
            )
        except PlaywrightTimeoutError:
            # Some JD pages visually open but do not emit domcontentloaded reliably in automation.
            logger.warning(
                "售后列表页导航超时，降级为 commit 导航 elapsed_ms=%s current_url=%s",
                int((time.monotonic() - nav_start) * 1000),
                page.url,
            )
            nav_start = time.monotonic()
            page.goto(_AFTERSALE_LIST_ALL_URL, wait_until="commit", timeout=20_000)
            logger.info(
                "售后列表页降级导航完成 wait_until=commit elapsed_ms=%s current_url=%s",
                int((time.monotonic() - nav_start) * 1000),
                page.url,
            )
        except PlaywrightError as exc:
            # JD redirects to the login page before domcontentloaded fires when the session
            # is already invalid. Playwright surfaces this as "Navigation ... interrupted by
            # another navigation to passport.shop.jd.com/login/...".  Convert it so the
            # reauth-retry wrapper in jd_scraper.py can handle it properly.
            err_msg = str(exc)
            if "interrupted by another navigation" in err_msg and "login" in err_msg.lower():
                logger.warning(
                    "售后列表页导航被登录跳转中断，Session 已过期 current_url=%s err=%s",
                    page.url,
                    err_msg[:200],
                )
                raise JDSessionExpiredError(
                    f"navigation to aftersale list interrupted by login redirect: {err_msg}"
                ) from exc
            raise

        if _AFTERSALE_PAGE_PATH_HINT not in (page.url or ""):
            logger.warning("售后列表页URL未命中预期路径 current_url=%s", page.url)
        if self._is_login_redirect_or_page(page):
            raise JDSessionExpiredError("redirected to login page when opening JD aftersale list")

        # Wait briefly for the first aftersale API packet. If none appears, avoid long silent waits.
        first_packet_deadline = time.monotonic() + 12
        while time.monotonic() < first_packet_deadline and response_page_count == 0:
            page.wait_for_timeout(300)

        if response_page_count == 0:
            logger.warning(
                "售后列表首包等待超时，直接启用DOM兜底 max_pages=%s current_url=%s",
                max_pages,
                page.url,
            )
            records = self._extract_by_dom(page, max_pages=max_pages)
            if not records and self._is_login_redirect_or_page(page):
                raise JDSessionExpiredError("session expired during JD aftersale list extraction")
            if on_page_aftersales is not None and records:
                on_page_aftersales(records)
            logger.info(
                "aftersale list extracted by dom-fallback aftersales=%s total_item=%s pages_clicked=%s response_pages=%s",
                len(records),
                total_item,
                1,
                response_page_count,
            )
            return records

        page.wait_for_timeout(3_000)
        if self._is_login_redirect_or_page(page):
            raise JDSessionExpiredError("session expired before JD aftersale list became ready")

        max_pages = max(1, int(max_pages))
        pages_fetched = 1

        while pages_fetched < max_pages:
            if total_item is not None and (pages_fetched * page_size) >= total_item:
                logger.info(
                    "售后达到总量上限，停止翻页 pages_fetched=%s page_size=%s total_item=%s",
                    pages_fetched,
                    page_size,
                    total_item,
                )
                break

            logger.info("售后翻页中 current_page=%s target_page=%s", pages_fetched, pages_fetched + 1)
            if not self._goto_next_page(page):
                logger.info("售后未找到下一页控件，停止翻页 current_page=%s", pages_fetched)
                break

            sleep_ms = random.randint(
                max(0, human_action_min_ms),
                max(human_action_min_ms, human_action_max_ms),
            )
            page.wait_for_timeout(sleep_ms + 3_000)
            pages_fetched += 1
            logger.info("售后翻页完成 current_page=%s wait_ms=%s", pages_fetched, sleep_ms + 3000)

        page.remove_listener("response", _on_response)

        all_aftersales: list[dict[str, Any]] = []
        for batch in captured_pages:
            all_aftersales.extend(batch)

        records = self._dedupe_aftersales(all_aftersales)
        logger.info(
            "aftersale list extracted aftersales=%s total_item=%s pages_clicked=%s response_pages=%s",
            len(records),
            total_item,
            pages_fetched,
            response_page_count,
        )

        if response_page_count == 0:
            logger.warning("no aftersale API response captured, enabling DOM fallback max_pages=%s", max_pages)
            records = self._extract_by_dom(page, max_pages=max_pages)

        if not records and self._is_login_redirect_or_page(page):
            raise JDSessionExpiredError("session expired during JD aftersale list extraction")

        if on_page_aftersales is not None and records and response_page_count == 0:
            on_page_aftersales(records)

        return records

    @staticmethod
    def _is_aftersale_response_url(url: str) -> bool:
        lower_url = (url or "").lower()
        if "shop.jd.com" not in lower_url and "sff.jd.com" not in lower_url:
            return False
        return any(fragment.lower() in lower_url for fragment in _AFTERSALE_API_FRAGMENTS)

    @staticmethod
    def _extract_aftersale_data(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        code = payload.get("code")
        if code not in (0, 200, "0", "200", None):
            return None

        candidates: list[Any] = [payload.get("data"), payload.get("result")]
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.extend([data.get("data"), data.get("result"), data.get("page"), data.get("pageData")])

        for item in candidates:
            if not isinstance(item, dict):
                continue
            results = item.get("results") or item.get("list") or item.get("records") or item.get("rows")
            if not isinstance(results, list) or not results:
                continue
            first = results[0]
            if not isinstance(first, dict):
                continue
            has_aftersale_shape = any(
                key in first
                for key in ("afterSaleId", "aftersaleId", "serviceId", "afterSaleStatus", "refundAmount")
            )
            if not has_aftersale_shape:
                continue
            normalized = dict(item)
            normalized["results"] = results
            return normalized
        return None

    @staticmethod
    def _dedupe_aftersales(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        for raw in raw_items:
            key = str(
                raw.get("afterSaleId")
                or raw.get("aftersaleId")
                or raw.get("serviceId")
                or raw.get("outer_aftersale_id")
                or raw.get("id")
                or ""
            )
            if not key:
                continue
            deduped[key] = raw
        return list(deduped.values())

    @staticmethod
    def _parse_type_from_text(text: str) -> str:
        """Extract JD aftersale type from raw page text."""
        # Check more specific patterns first
        _TYPE_KEYWORDS: tuple[tuple[str, str], ...] = (
            ("退货退款", "退货退款"),
            ("换货", "换货"),
            ("维修", "维修"),
            ("补件", "补件"),
            ("未收货退款", "未收货退款"),
            ("仅退款", "仅退款"),
            ("退款", "仅退款"),
        )
        for keyword, type_val in _TYPE_KEYWORDS:
            if keyword in text:
                return type_val
        return "UNKNOWN"

    @staticmethod
    def _parse_status_from_text(text: str) -> str:
        """Extract JD aftersale status from raw page text."""
        # Normalize spaces (JD sometimes renders "完 成" with a space)
        normalized = text.replace("\u3000", "").replace(" ", "")
        _STATUS_KEYWORDS: tuple[tuple[str, str], ...] = (
            ("退款成功", "退款成功"),
            ("完成", "完成"),
            ("退款关闭", "退款关闭"),
            ("已关闭", "已关闭"),
            ("已撤销", "已撤销"),
            ("商家拒绝", "商家拒绝"),
            ("退款中", "退款中"),
            ("商家同意", "商家同意"),
            ("审核中", "审核中"),
            ("商家处理中", "商家处理中"),
            ("申请退款", "申请退款"),
        )
        for keyword, status_val in _STATUS_KEYWORDS:
            if keyword in normalized:
                return status_val
        return "UNKNOWN"

    def _extract_by_dom(self, page: Page, *, max_pages: int) -> list[dict[str, Any]]:
        max_pages = max(1, int(max_pages))
        page_index = 1
        all_records: list[dict[str, Any]] = []

        while page_index <= max_pages:
            page_records = self._read_dom_rows(page)
            all_records.extend(page_records)
            logger.info("aftersale dom fallback page=%s records=%s", page_index, len(page_records))

            if page_index >= max_pages:
                break
            if not self._goto_next_page(page):
                break
            page.wait_for_timeout(2_500)
            page_index += 1

        return self._dedupe_aftersales(all_records)

    @staticmethod
    def _read_dom_rows(page: Page) -> list[dict[str, Any]]:
        targets = [page, *[frame for frame in page.frames if frame != page.main_frame]]
        rows: list[dict[str, Any]] = []
        pattern_after = re.compile(r"(?:售后单号|服务单号)\s*[:：]?\s*([A-Za-z0-9\-]{6,})")
        pattern_order = re.compile(r"(?:订单号|订单编号)\s*[:：]?\s*([A-Za-z0-9\-]{6,})")
        pattern_money = re.compile(r"[¥￥]\s*([\d\.,]+)")
        pattern_datetime = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

        for target in targets:
            try:
                texts = target.evaluate(
                    """
                    () => {
                      const candidates = Array.from(document.querySelectorAll('tr, .table-row, .list-row, .card, .service-list-item'));
                      return candidates.map(node => (node.innerText || '').trim()).filter(Boolean);
                    }
                    """
                )
            except Exception:
                continue

            if not isinstance(texts, list):
                continue

            for text in texts:
                if not isinstance(text, str) or len(text) < 8:
                    continue
                match_after = pattern_after.search(text)
                if not match_after:
                    continue
                match_order = pattern_order.search(text)
                match_money = pattern_money.search(text)
                match_dt = pattern_datetime.search(text)
                parsed_time = match_dt.group(1).replace("T", " ") if match_dt else None
                rows.append(
                    {
                        "afterSaleId": match_after.group(1),
                        "orderId": match_order.group(1) if match_order else "",
                        "refundAmount": match_money.group(1) if match_money else None,
                        "createTime": parsed_time,
                        "updateTime": parsed_time,
                        "type": JDAftersaleListExtractor._parse_type_from_text(text),
                        "status": JDAftersaleListExtractor._parse_status_from_text(text),
                        "raw_text": text[:800],
                    }
                )
        return rows

    @staticmethod
    def _goto_next_page(page: Page) -> bool:
        candidates = [
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
        for target in targets:
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
                        return True
                except Exception:
                    continue
        return False

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
