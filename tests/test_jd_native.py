"""Unit tests for scraper_client/infra/jd/ native modules."""
from __future__ import annotations

import pytest

from scraper_client.infra.jd.detail_extractor import JDDetailExtractor
from scraper_client.infra.jd.exceptions import JDParseError
from scraper_client.infra.jd.jd_scraper import JDScraper
from scraper_client.infra.jd.response_parser import (
    _yuan_to_cents,
    parse_item,
    parse_order,
    parse_price_info,
)
from scraper_client.services.scrape_executor import ScrapeExecutor, UnsupportedPlatformError


# ──────────────────────────────────────────────
# response_parser: _yuan_to_cents
# ──────────────────────────────────────────────


class TestYuanToCents:
    def test_float_string(self):
        assert _yuan_to_cents("99.90") == 9990

    def test_yuan_symbol_prefix(self):
        assert _yuan_to_cents("¥99.90") == 9990

    def test_full_width_yuan_symbol(self):
        assert _yuan_to_cents("￥12.50") == 1250

    def test_integer_string(self):
        assert _yuan_to_cents("100") == 10000

    def test_int_value(self):
        assert _yuan_to_cents(50) == 5000

    def test_float_value(self):
        assert _yuan_to_cents(1.23) == 123

    def test_none_returns_none(self):
        assert _yuan_to_cents(None) is None

    def test_empty_string_returns_none(self):
        assert _yuan_to_cents("") is None

    def test_invalid_string_returns_none(self):
        assert _yuan_to_cents("n/a") is None

    def test_comma_separated(self):
        assert _yuan_to_cents("1,234.56") == 123456


# ──────────────────────────────────────────────
# response_parser: parse_order
# ──────────────────────────────────────────────


class TestParseOrder:
    def test_minimal_payload(self):
        raw = {
            "orderId": "JD12345",
            "orderState": "4",
            "orderTotalPrice": "199.00",
        }
        order = parse_order(raw)
        assert order["outer_order_id"] == "JD12345"
        assert order["status"] == "4"
        assert order["total_amount"] == 19900
        assert order["raw_data"] is raw

    def test_alt_field_names(self):
        raw = {"outer_order_id": "ALT001", "status": "1", "total_amount": 10000}
        order = parse_order(raw)
        assert order["outer_order_id"] == "ALT001"
        assert order["total_amount"] == 10000

    def test_missing_order_id_raises(self):
        with pytest.raises(JDParseError):
            parse_order({"orderState": "1"})

    def test_receiver_fields(self):
        raw = {
            "orderId": "X1",
            "receiverName": "张三",
            "receiverMobile": "13800000000",
            "receiverAddress": "北京市",
        }
        order = parse_order(raw)
        assert order["receiver_name"] == "张三"
        assert order["receiver_phone"] == "13800000000"
        assert order["receiver_address"] == "北京市"


# ──────────────────────────────────────────────
# response_parser: parse_item
# ──────────────────────────────────────────────


class TestParseItem:
    def test_basic_item(self):
        raw = {
            "skuId": "SKU001",
            "skuName": "商品名称",
            "unitPrice": "49.90",
            "itemCount": 2,
            "totalPrice": "99.80",
        }
        item = parse_item("ORD001", raw)
        assert item["outer_order_id"] == "ORD001"
        assert item["product_id"] == "SKU001"
        assert item["unit_price"] == 4990
        assert item["quantity"] == 2
        assert item["total_price"] == 9980

    def test_price_fields_none_when_absent(self):
        raw = {"skuId": "S1", "skuName": "N", "unitPrice": "10", "itemCount": 1, "totalPrice": "10"}
        item = parse_item("O1", raw)
        assert item["sale_price"] is None
        assert item["merchant_discounted_price"] is None


# ──────────────────────────────────────────────
# response_parser: parse_price_info
# ──────────────────────────────────────────────


class TestParsePriceInfo:
    def test_merchant_receive_price(self):
        raw = {"merchantReceivePrice": "180.00"}
        pi = parse_price_info("ORD1", raw)
        assert pi["merchant_receive_price"] == 18000

    def test_typo_variant(self):
        # backend historically has merchant_receieve_price (typo)
        raw = {"merchant_receieve_price": "55.00"}
        pi = parse_price_info("ORD2", raw)
        assert pi["merchant_receive_price"] == 5500

    def test_extra_data_is_raw(self):
        raw = {"freight": "10.00"}
        pi = parse_price_info("ORD3", raw)
        assert pi["extra_data"] is raw


# ──────────────────────────────────────────────
# ScrapeExecutor routing
# ──────────────────────────────────────────────


class TestScrapeExecutorRouting:
    def test_unsupported_platform_raises(self):
        from scraper_client.core.settings import Settings

        executor = ScrapeExecutor(Settings())
        with pytest.raises(UnsupportedPlatformError):
            executor.execute_account({}, "WECHAT")

    def test_jingdong_routes_to_jd_scraper(self, monkeypatch):
        """JINGDONG platform must not raise UnsupportedPlatformError."""
        from scraper_client.core.settings import Settings

        monkeypatch.setattr(JDScraper, "scrape", lambda self, *_args, **_kwargs: ([{"outer_order_id": "1"}], [{"outer_order_id": "1"}], [{"outer_order_id": "1"}]))

        executor = ScrapeExecutor(Settings())
        orders, items, price_info = executor.execute_account(
            {"id": 1, "account_name": "test"},
            "JINGDONG",
        )
        assert isinstance(orders, list)
        assert isinstance(items, list)
        assert isinstance(price_info, list)


class TestJDDetailExtractor:
    def test_enrich_builds_fallback_items_and_price(self):
        raw_orders = [{"orderId": "O1", "orderTotalPrice": "88.00", "item_summary": "fallback item"}]
        enriched = JDDetailExtractor().enrich(raw_orders)
        assert len(enriched) == 1
        assert enriched[0]["orderItems"]
        assert enriched[0]["priceInfo"]["orderPrice"] == "88.00"


class TestJDScrapeConfig:
    def test_scrape_max_pages_supports_new_key(self):
        from scraper_client.core.settings import Settings

        scraper = JDScraper(Settings())
        cfg = scraper.scrape_config_from({"scrape_max_pages": 7, "human_action_min_ms": 11, "human_action_max_ms": 22})
        assert cfg["scrape_max_pages"] == 7
        assert cfg["human_action_min_ms"] == 11
        assert cfg["human_action_max_ms"] == 22
