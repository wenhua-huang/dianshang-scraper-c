from __future__ import annotations

from typing import Any

from scraper_client.domain.models import UploadAftersaleCounts
from scraper_client.infra.server.internal_api_client import InternalApiClient


class AftersaleUploader:
    def __init__(self, api_client: InternalApiClient) -> None:
        self.api_client = api_client

    def upload(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
        aftersales: list[dict[str, Any]],
    ) -> UploadAftersaleCounts:
        return self.api_client.upload_aftersales(
            round_no=round_no,
            platform=platform,
            shop_account_id=shop_account_id,
            client_id=client_id,
            aftersales=aftersales,
        )
