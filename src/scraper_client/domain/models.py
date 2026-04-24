from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ShopAccountInfo:
    id: int
    shop_id: int
    platform: str
    account_name: str
    account_password: str
    phone: str
    email: str | None
    is_active: bool = True
    latest_round_no: int = 0
    human_action_min_ms: int = 1000
    human_action_max_ms: int = 5000
    scrape_max_pages: int = 10


@dataclass(slots=True)
class ScrapeConfig:
    human_action_min_ms: int = 800
    human_action_max_ms: int = 2200
    max_pages: int = 5


@dataclass(slots=True)
class UploadResultCounts:
    orders_inserted: int = 0
    orders_updated: int = 0
    items_inserted: int = 0
    items_updated: int = 0
    price_info_inserted: int = 0
    price_info_updated: int = 0


@dataclass(slots=True)
class RunContext:
    round_no: int
    platform: str
    shop_account_id: int
    client_id: str


JSONDict = dict[str, Any]
