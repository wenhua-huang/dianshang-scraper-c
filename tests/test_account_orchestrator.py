from __future__ import annotations

import logging

from scraper_client.core.settings import Settings
from scraper_client.infra.jd.jd_scraper import JDScraper
from scraper_client.services.account_orchestrator import AccountOrchestrator


class _FakeResultUploader:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upload(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _build_orders(total: int) -> tuple[list[dict], list[dict], list[dict]]:
    orders: list[dict] = []
    items: list[dict] = []
    price_info: list[dict] = []

    for index in range(1, total + 1):
        outer_order_id = f"ORD-{index}"
        orders.append({"outer_order_id": outer_order_id})
        items.extend(
            [
                {"outer_order_id": outer_order_id, "sku": f"SKU-{index}-A"},
                {"outer_order_id": outer_order_id, "sku": f"SKU-{index}-B"},
            ]
        )
        price_info.append({"outer_order_id": outer_order_id, "amount": index * 100})

    return orders, items, price_info


def test_batch_upload_results_uses_batch_size_5_and_logs_progress(caplog):
    orchestrator = AccountOrchestrator(Settings())
    fake_uploader = _FakeResultUploader()
    orchestrator.result_uploader = fake_uploader
    orders, items, price_info = _build_orders(12)

    with caplog.at_level(logging.INFO):
        handler = orchestrator._build_stream_upload_handler(
            round_no=3,
            platform="JINGDONG",
            shop_account_id=123,
            client_id="test-client",
        )
        handler(orders[:5], items[:10], price_info[:5])
        handler(orders[5:10], items[10:20], price_info[5:10])
        handler(orders[10:12], items[20:24], price_info[10:12])

    assert [len(call["orders"]) for call in fake_uploader.calls] == [5, 5, 2]
    assert [len(call["items"]) for call in fake_uploader.calls] == [10, 10, 4]
    assert [len(call["price_info"]) for call in fake_uploader.calls] == [5, 5, 2]

    success_logs = [
        record.message for record in caplog.records if "批次上传成功" in record.message
    ]
    assert len(success_logs) == 3
    assert any("cumulative_orders=5" in message for message in success_logs)
    assert any("cumulative_orders=10" in message for message in success_logs)
    assert any("cumulative_orders=12" in message for message in success_logs)
    assert any(
        "批次上传成功 batch_num=3" in record.message
        for record in caplog.records
    )


class _FakeSessionManager:
    def open_session(self, _account_id: int):
        return type("Session", (), {"page": object()})()

    def close_session(self, _session, *, persist_state: bool = True) -> None:
        return None


class _FakeAuthenticator:
    def ensure_authenticated(self, *args, **kwargs) -> None:
        return None


class _FakeListExtractor:
    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages

    def extract(self, _page, **kwargs):
        callback = kwargs.get("on_page_orders")
        all_orders: list[dict] = []
        for page in self.pages:
            if callback is not None:
                callback(page)
            all_orders.extend(page)
        return all_orders


def test_jd_scraper_streams_batches_after_each_threshold():
    scraper = JDScraper(Settings())
    scraper._session_manager = _FakeSessionManager()
    scraper._authenticator = _FakeAuthenticator()
    scraper._list_extractor = _FakeListExtractor(
        [
            [{"orderId": "O1", "orderTotalPrice": "10.00"}, {"orderId": "O2", "orderTotalPrice": "20.00"}],
            [{"orderId": "O3", "orderTotalPrice": "30.00"}, {"orderId": "O4", "orderTotalPrice": "40.00"}],
            [{"orderId": "O5", "orderTotalPrice": "50.00"}],
        ]
    )

    uploaded_batches: list[tuple[list[dict], list[dict], list[dict]]] = []

    orders, items, price_info = scraper.scrape(
        {"id": 1, "account_name": "acct"},
        {"scrape_max_pages": 99, "upload_batch_size": 2},
        on_batch_ready=lambda batch_orders, batch_items, batch_price: uploaded_batches.append(
            (batch_orders, batch_items, batch_price)
        ),
    )

    assert [len(batch_orders) for batch_orders, _, _ in uploaded_batches] == [2, 2, 1]
    assert [len(batch_items) for _, batch_items, _ in uploaded_batches] == [2, 2, 1]
    assert [len(batch_price) for _, _, batch_price in uploaded_batches] == [2, 2, 1]
    assert len(orders) == 5
    assert len(items) == 5
    assert len(price_info) == 5