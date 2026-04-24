from __future__ import annotations

from typing import Any

from scraper_client.domain.models import UploadResultCounts
from scraper_client.infra.server.internal_api_client import InternalApiClient


class ResultUploader:
    def __init__(self, api_client: InternalApiClient) -> None:
        self.api_client = api_client

    def upload(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
        orders: list[dict[str, Any]],
        items: list[dict[str, Any]],
        price_info: list[dict[str, Any]],
    ) -> UploadResultCounts:
        return self.api_client.upload_results(
            round_no=round_no,
            platform=platform,
            shop_account_id=shop_account_id,
            client_id=client_id,
            orders=orders,
            items=items,
            price_info=price_info,
        )
