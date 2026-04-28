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
        logger.info("开始补全订单详情 raw_orders=%s", len(raw_orders))
        enriched: list[dict[str, Any]] = []
        inferred_count = 0
        embedded_detail_count = 0
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
            if items:
                embedded_detail_count += 1
                logger.info(
                    "列表数据已含商品详情，跳过详情页请求 order_id=%s item_count=%s",
                    order_id,
                    len(items),
                )
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
                inferred_count += 1
                logger.info("缺少商品详情，使用推断数据 order_id=%s", order_id)
            order["orderItems"] = items

            if not (order.get("priceInfo") or order.get("price_info") or order.get("orderPaymentInfo")):
                order["priceInfo"] = {
                    "orderPrice": order.get("orderTotalPrice") or order.get("totalPrice") or "0",
                    "productPrice": order.get("orderTotalPrice") or order.get("totalPrice") or "0",
                    "merchantReceivePrice": order.get("orderTotalPrice") or order.get("totalPrice") or "0",
                }

            enriched.append(order)

        logger.info(
            "订单详情补全完成 enriched_orders=%s inferred_orders=%s embedded_detail_orders=%s",
            len(enriched),
            inferred_count,
            embedded_detail_count,
        )
        return enriched
