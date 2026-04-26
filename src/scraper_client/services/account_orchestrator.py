from __future__ import annotations

import logging
import signal
import time
from dataclasses import asdict

from scraper_client.core.settings import Settings
from scraper_client.domain.models import ShopAccountInfo
from scraper_client.infra.server.internal_api_client import InternalApiClient
from scraper_client.services.result_uploader import ResultUploader
from scraper_client.services.run_log_uploader import RunLogUploader
from scraper_client.services.scrape_executor import ScrapeExecutor

logger = logging.getLogger(__name__)


class AccountOrchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_client = InternalApiClient(
            base_url=settings.scraper_server_base_url,
            api_key=settings.scraper_internal_api_key,
        )
        self.executor = ScrapeExecutor(settings)
        self.result_uploader = ResultUploader(self.api_client)
        self.log_uploader = RunLogUploader(self.api_client)
        self._shutdown_requested = False

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    def register_signal_handlers(self) -> None:
        def _handle_signal(signum, _frame):
            logger.info("received signal=%s, will stop after current account", signum)
            self.request_shutdown()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    def run_forever(self) -> None:
        """Main loop: fetch tasks from backend and execute them.
        
        Blocking behavior:
        - If any account is running, /task returns None
        - Sleep and retry until account completes
        """
        retry_count = 0
        while not self._shutdown_requested:
            try:
                task = self._fetch_task()
                if task is None:
                    retry_count = 0
                    logger.info("no task available, sleep=%ss", self.settings.empty_queue_backoff_seconds)
                    time.sleep(self.settings.empty_queue_backoff_seconds)
                    continue

                self.execute_task(task)
                retry_count = 0
                time.sleep(self.settings.poll_interval_seconds)
            except Exception as exc:
                retry_count += 1
                backoff = min(
                    self.settings.retry_backoff_max_seconds,
                    self.settings.empty_queue_backoff_seconds * (2 ** max(retry_count - 1, 0)),
                )
                logger.exception(
                    "daemon cycle failed retries=%s/%s backoff=%ss err=%s",
                    retry_count,
                    self.settings.max_retry_attempts,
                    backoff,
                    exc,
                )
                if retry_count >= self.settings.max_retry_attempts:
                    logger.error("max retry attempts reached, continue in low-frequency mode")
                    retry_count = 0
                    backoff = self.settings.retry_backoff_max_seconds
                time.sleep(backoff)

    def _fetch_task(self) -> dict | None:
        """Fetch next task from backend /task endpoint."""
        try:
            task = self.api_client.get_task()
            return task
        except Exception as exc:
            logger.exception("failed to fetch task: %s", exc)
            raise

    def execute_account(self, account: ShopAccountInfo) -> None:
        """Legacy method for backward compatibility. Use execute_task instead."""
        account_dict = asdict(account)
        round_no = account.current_round if hasattr(account, 'current_round') else max(1, int(account.latest_round_no or 0) + 1)
        error_message: str | None = None
        order_count = 0

        try:
            orders, items, price_info = self.executor.execute_account(
                account_dict,
                account.platform,
                scrape_config={
                    "human_action_min_ms": account.human_action_min_ms,
                    "human_action_max_ms": account.human_action_max_ms,
                    "scrape_max_pages": account.scrape_max_pages,
                    "max_pages": account.scrape_max_pages,
                },
            )
            order_count = len(orders)

            self.result_uploader.upload(
                round_no=round_no,
                platform=account.platform,
                shop_account_id=account.id,
                client_id=self.settings.scraper_client_id,
                orders=orders,
                items=items,
                price_info=price_info,
            )

            self.log_uploader.upload(
                round_no=round_no,
                platform=account.platform,
                shop_account_id=account.id,
                client_id=self.settings.scraper_client_id,
                run_status="SUCCESS",
                order_count=order_count,
                log_data={"account_id": account.id},
            )

            logger.info("account success account_id=%s orders=%s", account.id, order_count)
        except Exception as exc:
            error_message = str(exc)[:500]
            logger.exception("account failed account_id=%s", account.id)

            try:
                self.log_uploader.upload(
                    round_no=round_no,
                    platform=account.platform,
                    shop_account_id=account.id,
                    client_id=self.settings.scraper_client_id,
                    run_status="FAILED",
                    order_count=order_count,
                    error_message=error_message,
                    log_data={"account_id": account.id},
                )
            except Exception:
                logger.exception("upload failed run log failed account_id=%s", account.id)

    def execute_task(self, task: dict) -> None:
        """Execute scraping task from backend /task endpoint."""
        account_id = task.get("id")
        round_no = task.get("current_round", 1)
        error_message: str | None = None
        order_count = 0

        try:
            # Create account dict from task
            account_dict = {
                "id": task.get("id"),
                "shop_id": task.get("shop_id"),
                "platform": task.get("platform"),
                "account_name": task.get("account_name"),
                "account_password": task.get("account_password"),
                "phone": task.get("phone"),
                "email": task.get("email"),
                "is_active": True,
                "latest_round_no": round_no,
                "human_action_min_ms": task.get("human_action_min_ms", 1000),
                "human_action_max_ms": task.get("human_action_max_ms", 5000),
                "scrape_max_pages": task.get("scrape_max_pages", 10),
            }

            orders, items, price_info = self.executor.execute_account(
                account_dict,
                task.get("platform"),
                scrape_config={
                    "human_action_min_ms": task.get("human_action_min_ms", 1000),
                    "human_action_max_ms": task.get("human_action_max_ms", 5000),
                    "scrape_max_pages": task.get("scrape_max_pages", 10),
                    "max_pages": task.get("scrape_max_pages", 10),
                },
            )
            order_count = len(orders)

            self.result_uploader.upload(
                round_no=round_no,
                platform=task.get("platform"),
                shop_account_id=account_id,
                client_id=self.settings.scraper_client_id,
                orders=orders,
                items=items,
                price_info=price_info,
            )

            self.log_uploader.upload(
                round_no=round_no,
                platform=task.get("platform"),
                shop_account_id=account_id,
                client_id=self.settings.scraper_client_id,
                run_status="SUCCESS",
                order_count=order_count,
                log_data={"account_id": account_id},
            )

            logger.info("task success account_id=%s orders=%s round=%s", account_id, order_count, round_no)
        except Exception as exc:
            error_message = str(exc)[:500]
            logger.exception("task failed account_id=%s round=%s", account_id, round_no)

            try:
                self.log_uploader.upload(
                    round_no=round_no,
                    platform=task.get("platform"),
                    shop_account_id=account_id,
                    client_id=self.settings.scraper_client_id,
                    run_status="FAILED",
                    order_count=order_count,
                    error_message=error_message,
                    log_data={"account_id": account_id},
                )
            except Exception:
                logger.exception("upload failed run log failed account_id=%s", account_id)
