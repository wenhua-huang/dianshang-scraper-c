from __future__ import annotations

from typing import Any

from scraper_client.infra.server.internal_api_client import InternalApiClient


class RunLogUploader:
    def __init__(self, api_client: InternalApiClient) -> None:
        self.api_client = api_client

    def upload(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
        run_status: str,
        order_count: int,
        error_message: str | None = None,
        log_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.api_client.upload_run_log(
            round_no=round_no,
            platform=platform,
            shop_account_id=shop_account_id,
            client_id=client_id,
            run_status=run_status,
            order_count=order_count,
            error_message=error_message,
            log_data=log_data,
        )
