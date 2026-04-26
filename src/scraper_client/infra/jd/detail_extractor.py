from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class JDDetailExtractor:
    """Build item and price blocks from raw order payloads.

    Phase-2 strategy:
    - Prefer native fields embedded in order payload: skuInfos/items/priceInfo.
    - If details are missing, create inferred records so uploader always gets
      structured items + price_info for each order.
    """

    def enrich(self, raw_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for raw in raw_orders:
            order_id = str(
                raw.get("orderId")
                or raw.get("orderNo")
                or raw.get("order_no")
                or raw.get("outer_order_id")
                or ""
            )
            if not order_id:
                continue

            order = dict(raw)
            items = order.get("orderItems") or order.get("skuInfos") or order.get("items") or []
            if not items:
                # infer a single item from order summary when detailed items are absent
                payment = order.get("orderPaymentInfo") or {}
                unit_price_yuan = (
                    payment.get("receivables")
                    or order.get("orderTotalPrice")
                    or order.get("totalPrice")
                    or "0"
                )
                items = [
                    {
                        "skuId": f"INFER-{order_id}",
                        "skuName": order.get("item_summary") or "UNKNOWN",
                        "num": 1,
                        "jdPrice": unit_price_yuan,
                    }
                ]
            order["orderItems"] = items

            if not (order.get("priceInfo") or order.get("price_info") or order.get("orderPaymentInfo")):
                order["priceInfo"] = {
                    "orderPrice": order.get("orderTotalPrice") or order.get("totalPrice") or "0",
                    "productPrice": order.get("orderTotalPrice") or order.get("totalPrice") or "0",
                    "merchantReceivePrice": order.get("orderTotalPrice") or order.get("totalPrice") or "0",
                }

            enriched.append(order)

        logger.info("detail extraction enriched_orders=%s", len(enriched))
        return enriched
