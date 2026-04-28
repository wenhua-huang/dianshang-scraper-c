"""Normalise raw JD API / DOM payloads into the backend contract schema.

Backend expected schemas
------------------------
Order record
    outer_order_id          str            JD order ID
    status                  str            JD status code ("0"-"6", "-1")
    platform_create_time    ISO-8601 str   optional
    platform_update_time    ISO-8601 str   optional
    total_amount            int            cents; 0 when unknown
    openid                  str | None
    receiver_name           str | None
    receiver_phone          str | None
    receiver_address        str | None
    raw_data                dict           verbatim source payload

OrderItem record
    outer_order_id          str
    product_id              str
    product_name            str
    sku_id                  str | None
    sku_name                str | None
    unit_price              int            cents
    quantity                int
    total_price             int            cents
    sale_price              int | None
    real_price              int | None
    estimate_price          int | None
    merchant_discounted_price   int | None
    finder_discounted_price     int | None
    deduction_price         int | None
    raw_data                dict

PriceInfo record
    outer_order_id          str
    product_price           int | None     cents
    order_price             int | None
    freight                 int | None
    discounted_price        int | None
    original_order_price    int | None
    merchant_receive_price  int | None
    extra_data              dict
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from scraper_client.infra.jd.exceptions import JDParseError

logger = logging.getLogger(__name__)

LABELED_DATETIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*([^\d,，;；]*)")


JD_TEXT_STATUS_CODE_MAP: tuple[tuple[str, str], ...] = (
    ("待付款", "0"),
    ("未付款", "0"),
    ("待出库", "1"),
    ("待发货", "1"),
    ("待揽收", "1"),
    ("已出库", "2"),
    ("已发货", "2"),
    ("运输中", "2"),
    ("配送中", "2"),
    ("待收货", "3"),
    ("已签收", "4"),
    ("交易完成", "4"),
    ("已完成", "4"),
    ("已取消", "5"),
    ("已退款", "6"),
    ("退款成功", "6"),
    ("已关闭", "-1"),
)


def _yuan_to_cents(value: Any) -> int | None:
    """Convert a yuan string/float/int to integer cents.

    Returns None when the value is absent or cannot be parsed.
    """
    if value is None:
        return None
    try:
        # Strip currency symbol if present
        cleaned = str(value).strip().lstrip("¥￥").replace(",", "")
        if not cleaned:
            return None
        return round(float(cleaned) * 100)
    except (ValueError, TypeError):
        logger.debug("无法转换为分（金额单位）: %r", value)
        return None


def _ms_to_mysql_datetime(ms: Any) -> str | None:
    """Convert a millisecond UNIX timestamp to MySQL DATETIME string (UTC)."""
    if ms is None:
        return None
    try:
        ts = int(ms) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def _normalize_datetime(value: Any) -> str | None:
    """Normalize various datetime representations to MySQL DATETIME string."""
    if value is None:
        return None

    # Millisecond timestamp as int/float.
    if isinstance(value, (int, float)) and int(value) > 1_000_000_000_000:
        return _ms_to_mysql_datetime(value)

    text = str(value).strip()
    if not text:
        return None

    # Millisecond timestamp encoded as string.
    if text.isdigit() and len(text) >= 13:
        return _ms_to_mysql_datetime(text)

    # ISO-8601 with trailing Z (UTC) -> +00:00 for fromisoformat.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Fallback: already a plain datetime string or unknown format; keep as-is.
        return str(value)

    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_jd_status(value: Any) -> str:
    """Normalize JD status values to backend-recognized raw status codes.

    JD list pages often expose Chinese text states such as "待出库" while the
    backend currently maps JD raw statuses using numeric codes.
    """
    if value is None:
        return ""

    status = str(value).strip()
    if not status:
        return ""

    if status in {"-1", "0", "1", "2", "3", "4", "5", "6", "8"}:
        return status

    for text, code in JD_TEXT_STATUS_CODE_MAP:
        if text in status:
            return code

    return status


def _extract_labeled_datetime(value: Any, *labels: str) -> str | None:
    """Extract a datetime preceding any of the given labels from mixed UI text."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    for match in LABELED_DATETIME_RE.finditer(text):
        dt_text = match.group(1)
        suffix = match.group(2).strip()
        if any(label in suffix for label in labels):
            return _normalize_datetime(dt_text)
    return None


def parse_order(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse a single raw JD order dict into the backend order record.

    Handles both the new sff.jd.com API format (orderStatusInfo, orderPaymentInfo,
    orderConsigneeInfo) and the legacy field names for backward compatibility.
    """
    outer_order_id = str(raw.get("orderId") or raw.get("outer_order_id") or "")
    if not outer_order_id:
        raise JDParseError(f"missing orderId in raw order: {list(raw.keys())}")

    # --- status ---
    status_info = raw.get("orderStatusInfo") or {}
    status = _normalize_jd_status(
        status_info.get("orderStatus")
        or raw.get("orderState")
        or raw.get("status")
        or ""
    )

    # --- timestamps ---
    # new API uses millisecond timestamps; DOM fallback may return mixed text
    create_source = raw.get("orderCreateTime") or raw.get("orderTime") or raw.get("platform_create_time")
    pay_source = (
        raw.get("paymentConfirmTime")
        or raw.get("payTime")
        or raw.get("paymentTime")
        or raw.get("pay_time")
        or create_source
    )
    update_source = raw.get("modifyTime") or raw.get("platform_update_time") or raw.get("paymentConfirmTime")

    platform_create_time = (
        _extract_labeled_datetime(create_source, "下单", "创建")
        or _normalize_datetime(create_source)
    )
    pay_time = (
        _extract_labeled_datetime(pay_source, "付款", "支付")
        or _normalize_datetime(pay_source)
    )
    platform_update_time = _normalize_datetime(update_source) or pay_time

    # Backend columns are NOT NULL; fall back to keep insert compatible.
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if platform_create_time is None:
        platform_create_time = platform_update_time or now_utc
    if platform_update_time is None:
        platform_update_time = platform_create_time

    # --- total amount ---
    payment_info = raw.get("orderPaymentInfo") or {}
    total_amount: int
    if payment_info.get("receivables") is not None:
        total_amount = _yuan_to_cents(payment_info["receivables"]) or 0
    elif payment_info.get("shouldPay") is not None:
        total_amount = _yuan_to_cents(payment_info["shouldPay"]) or 0
    elif raw.get("orderTotalPrice") is not None:
        total_amount = _yuan_to_cents(raw["orderTotalPrice"]) or 0
    elif raw.get("totalPrice") is not None:
        total_amount = _yuan_to_cents(raw["totalPrice"]) or 0
    elif raw.get("total_amount") is not None:
        total_amount = int(raw["total_amount"])
    else:
        total_amount = 0

    # --- consignee ---
    consignee = raw.get("orderConsigneeInfo") or {}
    receiver_name = (
        consignee.get("consName")
        or raw.get("receiverName")
        or raw.get("receiver_name")
    )
    receiver_phone = (
        consignee.get("consMobilePhone")
        or raw.get("receiverMobile")
        or raw.get("receiver_phone")
    )
    receiver_address = (
        consignee.get("consAddress")
        or raw.get("receiverAddress")
        or raw.get("receiver_address")
    )

    return {
        "outer_order_id": outer_order_id,
        "status": status,
        "platform_create_time": platform_create_time,
        "platform_update_time": platform_update_time,
        "total_amount": total_amount,
        "openid": raw.get("openid") or raw.get("userPin") or raw.get("buyerAccount"),
        "receiver_name": receiver_name,
        "receiver_phone": receiver_phone,
        "receiver_address": receiver_address,
        "pay_time": pay_time,
        "raw_data": raw,
    }


def parse_item(outer_order_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Parse a single raw JD order item into the backend OrderItem record.

    Handles both new API format (jdPrice, num) and legacy format (unitPrice, itemCount).
    """
    product_id = str(raw.get("skuId") or raw.get("product_id") or "")
    product_name = str(raw.get("skuName") or raw.get("product_name") or "")
    # new API: jdPrice (yuan float); legacy: unitPrice
    unit_price = (
        _yuan_to_cents(raw.get("jdPrice"))
        or _yuan_to_cents(raw.get("unitPrice") or raw.get("unit_price"))
        or 0
    )
    # new API: num; legacy: itemCount / quantity
    quantity = int(raw.get("num") or raw.get("itemCount") or raw.get("quantity") or 1)
    # new API doesn't include explicit totalPrice; derive it
    total_price = (
        _yuan_to_cents(raw.get("totalPrice") or raw.get("total_price"))
        or unit_price * quantity
    )

    return {
        "outer_order_id": outer_order_id,
        "product_id": product_id,
        "product_name": product_name,
        "sku_id": raw.get("skuId") or raw.get("sku_id"),
        "sku_name": raw.get("skuName") or raw.get("sku_name"),
        "unit_price": unit_price,
        "quantity": quantity,
        "total_price": total_price,
        "sale_price": _yuan_to_cents(raw.get("salePrice") or raw.get("sale_price")),
        "real_price": _yuan_to_cents(raw.get("realPrice") or raw.get("real_price")),
        "estimate_price": _yuan_to_cents(raw.get("estimatePrice") or raw.get("estimate_price")),
        "merchant_discounted_price": _yuan_to_cents(
            raw.get("merchantDiscountPrice") or raw.get("merchant_discounted_price")
        ),
        "finder_discounted_price": _yuan_to_cents(
            raw.get("finderDiscountPrice") or raw.get("finder_discounted_price")
        ),
        "deduction_price": _yuan_to_cents(raw.get("deductionPrice") or raw.get("deduction_price")),
        "raw_data": raw,
    }


def parse_price_info(outer_order_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Parse raw price breakdown into the backend PriceInfo record.

    Handles both new API format (orderPaymentInfo sub-object) and legacy flat fields.
    """
    payment = raw.get("orderPaymentInfo") or {}
    return {
        "outer_order_id": outer_order_id,
        "product_price": (
            _yuan_to_cents(payment.get("orderSum"))
            or _yuan_to_cents(raw.get("productPrice") or raw.get("product_price"))
        ),
        "order_price": (
            _yuan_to_cents(payment.get("shouldPay"))
            or _yuan_to_cents(raw.get("orderPrice") or raw.get("order_price"))
        ),
        "freight": (
            _yuan_to_cents(payment.get("freight"))
            or _yuan_to_cents(raw.get("freight") or raw.get("freightPrice"))
        ),
        "discounted_price": _yuan_to_cents(
            raw.get("discountPrice") or raw.get("discounted_price")
        ),
        "original_order_price": _yuan_to_cents(
            raw.get("originalOrderPrice") or raw.get("original_order_price")
        ),
        "merchant_receive_price": (
            _yuan_to_cents(payment.get("receivables"))
            or _yuan_to_cents(
                raw.get("merchantReceivePrice")
                or raw.get("merchant_receive_price")
                or raw.get("merchant_receieve_price")  # typo variant seen in some API responses
            )
        ),
        "extra_data": raw,
    }
