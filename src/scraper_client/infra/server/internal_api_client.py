from __future__ import annotations

import json
import ssl
from typing import Any
from urllib import error, parse, request

import certifi

from scraper_client.domain.models import ShopAccountInfo, UploadAftersaleCounts, UploadResultCounts


class InternalApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 20,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    def list_accounts(self, platform: str | None = None) -> list[ShopAccountInfo]:
        query = {"platform": platform} if platform else None
        payload = self._request_json("GET", "/shops/internal/accounts", query=query)
        items = payload.get("items", [])
        return [ShopAccountInfo(**item) for item in items]

    def upload_results(
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
        payload = {
            "round_no": round_no,
            "platform": platform,
            "shop_account_id": shop_account_id,
            "client_id": client_id,
            "orders": orders,
            "items": items,
            "price_info": price_info,
        }
        data = self._request_json("POST", "/internal/scraper/results/upload", payload)
        return UploadResultCounts(**data)

    def upload_run_log(
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
        payload = {
            "round_no": round_no,
            "platform": platform,
            "shop_account_id": shop_account_id,
            "client_id": client_id,
            "run_status": run_status,
            "order_count": order_count,
            "error_message": error_message,
            "log_data": log_data,
        }
        return self._request_json("POST", "/internal/scraper/logs/upload", payload)

    def upload_aftersales(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
        aftersales: list[dict[str, Any]],
    ) -> UploadAftersaleCounts:
        payload = {
            "round_no": round_no,
            "platform": platform,
            "shop_account_id": shop_account_id,
            "client_id": client_id,
            "aftersales": aftersales,
        }
        data = self._request_json("POST", "/internal/scraper/aftersales/upload", payload)
        return UploadAftersaleCounts(**data)

    def get_task(self) -> dict[str, Any] | None:
        """Fetch next available task from backend.
        
        Returns None if no task available or any account is running.
        """
        try:
            return self._request_json("GET", "/internal/scraper/task")
        except RuntimeError as exc:
            # If we get 404 or similar, task endpoint not available
            if "404" in str(exc):
                return None
            raise

    def create_verification_session(self, account_id: int, source: str = "SCRAPER") -> dict[str, Any]:
        payload = {
            "account_id": account_id,
            "source": source,
        }
        return self._request_json("POST", "/shops/internal/verification-sessions", payload)

    def get_verification_status(self, request_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/shops/internal/verification-sessions/{request_id}")

    def consume_verification_code(self, request_id: str) -> dict[str, Any]:
        return self._request_json("POST", f"/shops/internal/verification-sessions/{request_id}/consume", {})

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{parse.urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url=url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Scraper-Key", self.api_key)

        try:
            if self.verify_ssl:
                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            else:
                ssl_ctx = ssl._create_unverified_context()
            with request.urlopen(req, context=ssl_ctx, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"internal api error {exc.code} url={url}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"internal api unavailable url={url}: {exc.reason}") from exc

        if not text:
            return {}
        return json.loads(text)
