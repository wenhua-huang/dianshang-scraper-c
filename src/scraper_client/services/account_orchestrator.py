from __future__ import annotations

import logging
import random
import signal
import time
from dataclasses import asdict
from typing import Any, Callable

from scraper_client.core.settings import Settings
from scraper_client.domain.models import ShopAccountInfo
from scraper_client.infra.server.internal_api_client import InternalApiClient
from scraper_client.services.aftersale_uploader import AftersaleUploader
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
            verify_ssl=settings.scraper_verify_ssl,
        )
        self.executor = ScrapeExecutor(settings)
        self.result_uploader = ResultUploader(self.api_client)
        self.aftersale_uploader = AftersaleUploader(self.api_client)
        self.log_uploader = RunLogUploader(self.api_client)
        self._shutdown_requested = False

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    def register_signal_handlers(self) -> None:
        def _handle_signal(signum, _frame):
            logger.info("收到信号=%s，当前账号处理完后停止", signum)
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
                    sleep_secs = self.settings.empty_queue_backoff_seconds
                    logger.info("暂无任务，休眠 sleeping_secs=%s 秒", sleep_secs)
                    time.sleep(sleep_secs)
                    continue

                self.execute_task(task)
                retry_count = 0
                sleep_secs = self.settings.poll_interval_seconds
                logger.info("任务完成，休眠 sleeping_secs=%s 秒", sleep_secs)
                time.sleep(sleep_secs)
            except Exception as exc:
                retry_count += 1
                backoff = min(
                    self.settings.retry_backoff_max_seconds,
                    self.settings.empty_queue_backoff_seconds * (2 ** max(retry_count - 1, 0)),
                )
                logger.exception(
                    "后台循环出错 retries=%s/%s backoff=%ss err=%s",
                    retry_count,
                    self.settings.max_retry_attempts,
                    backoff,
                    exc,
                )
                if retry_count >= self.settings.max_retry_attempts:
                    logger.error("达到最大重试次数，切换低频模式")
                    retry_count = 0
                    backoff = self.settings.retry_backoff_max_seconds
                logger.info("后台循环错误，休眠 sleeping_secs=%s 秒", backoff)
                time.sleep(backoff)

    def _fetch_task(self) -> dict | None:
        """Fetch next task from backend /task endpoint."""
        try:
            task = self.api_client.get_task()
            return task
        except Exception as exc:
            logger.exception("获取任务失败: %s", exc)
            raise

    def _upload_result_batch(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
        orders: list[dict[str, Any]],
        items: list[dict[str, Any]],
        price_info: list[dict[str, Any]],
    ) -> None:
        if not orders:
            logger.info(
                "批次上传跳过（无订单）account_id=%s round=%s",
                shop_account_id,
                round_no,
            )
            return

        self.result_uploader.upload(
            round_no=round_no,
            platform=platform,
            shop_account_id=shop_account_id,
            client_id=client_id,
            orders=orders,
            items=items,
            price_info=price_info,
        )

    @staticmethod
    def _build_batch_logger(
        *,
        shop_account_id: int,
        round_no: int,
    ) -> Callable[[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]], None]:
        batch_num = 0
        uploaded_orders = 0
        uploaded_items = 0
        uploaded_price_info = 0

        def _log_batch(
            orders: list[dict[str, Any]],
            items: list[dict[str, Any]],
            price_info: list[dict[str, Any]],
        ) -> None:
            nonlocal batch_num, uploaded_orders, uploaded_items, uploaded_price_info
            batch_num += 1
            uploaded_orders += len(orders)
            uploaded_items += len(items)
            uploaded_price_info += len(price_info)
            logger.info(
                "批次上传成功 batch_num=%s batch_orders=%s batch_items=%s batch_price_info=%s cumulative_orders=%s cumulative_items=%s cumulative_price_info=%s account_id=%s round=%s",
                batch_num,
                len(orders),
                len(items),
                len(price_info),
                uploaded_orders,
                uploaded_items,
                uploaded_price_info,
                shop_account_id,
                round_no,
            )

        return _log_batch

    def _build_stream_upload_handler(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
    ) -> Callable[[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]], None]:
        log_batch = self._build_batch_logger(shop_account_id=shop_account_id, round_no=round_no)

        def _handle_batch(
            orders: list[dict[str, Any]],
            items: list[dict[str, Any]],
            price_info: list[dict[str, Any]],
        ) -> None:
            self._upload_result_batch(
                round_no=round_no,
                platform=platform,
                shop_account_id=shop_account_id,
                client_id=client_id,
                orders=orders,
                items=items,
                price_info=price_info,
            )
            log_batch(orders, items, price_info)

        return _handle_batch

    def _upload_aftersale_batch(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
        aftersales: list[dict[str, Any]],
    ) -> None:
        if not aftersales:
            logger.info(
                "售后批次上传跳过（无售后）account_id=%s round=%s",
                shop_account_id,
                round_no,
            )
            return

        self.aftersale_uploader.upload(
            round_no=round_no,
            platform=platform,
            shop_account_id=shop_account_id,
            client_id=client_id,
            aftersales=aftersales,
        )

    @staticmethod
    def _build_aftersale_batch_logger(*, shop_account_id: int, round_no: int) -> Callable[[list[dict[str, Any]]], None]:
        batch_num = 0
        uploaded_aftersales = 0

        def _log_batch(aftersales: list[dict[str, Any]]) -> None:
            nonlocal batch_num, uploaded_aftersales
            batch_num += 1
            uploaded_aftersales += len(aftersales)
            logger.info(
                "售后批次上传成功 batch_num=%s batch_aftersales=%s cumulative_aftersales=%s account_id=%s round=%s",
                batch_num,
                len(aftersales),
                uploaded_aftersales,
                shop_account_id,
                round_no,
            )

        return _log_batch

    def _build_aftersale_stream_upload_handler(
        self,
        *,
        round_no: int,
        platform: str,
        shop_account_id: int,
        client_id: str,
    ) -> Callable[[list[dict[str, Any]]], None]:
        log_batch = self._build_aftersale_batch_logger(shop_account_id=shop_account_id, round_no=round_no)

        def _handle_batch(aftersales: list[dict[str, Any]]) -> None:
            self._upload_aftersale_batch(
                round_no=round_no,
                platform=platform,
                shop_account_id=shop_account_id,
                client_id=client_id,
                aftersales=aftersales,
            )
            log_batch(aftersales)

        return _handle_batch

    def execute_account(self, account: ShopAccountInfo) -> None:
        """Legacy method for backward compatibility. Use execute_task instead."""
        account_dict = asdict(account)
        round_no = account.current_round if hasattr(account, 'current_round') else max(1, int(account.latest_round_no or 0) + 1)
        order_error: str | None = None
        aftersale_error: str | None = None
        order_count = 0
        aftersale_count = 0

        try:
            upload_batch_size = random.randint(20, 50)
            logger.info(
                "账号流式上传配置 batch_size=%s account_id=%s round=%s",
                upload_batch_size,
                account.id,
                round_no,
            )
            base_scrape_config = {
                "human_action_min_ms": account.human_action_min_ms,
                "human_action_max_ms": account.human_action_max_ms,
                "scrape_max_pages": account.scrape_max_pages,
                "aftersale_max_pages": account.aftersale_max_pages,
                "max_pages": account.scrape_max_pages,
                "upload_batch_size": upload_batch_size,
            }

            try:
                orders, items, price_info = self.executor.execute_account(
                    account_dict,
                    account.platform,
                    scrape_config=base_scrape_config,
                    on_batch_ready=self._build_stream_upload_handler(
                        round_no=round_no,
                        platform=account.platform,
                        shop_account_id=account.id,
                        client_id=self.settings.scraper_client_id,
                    ),
                )
                order_count = len(orders)
                logger.info("账号订单抓取成功 account_id=%s orders=%s", account.id, order_count)
            except Exception as exc:
                order_error = str(exc)[:500]
                logger.exception("账号订单抓取失败，继续执行售后抓取 account_id=%s", account.id)

            try:
                aftersales = self.executor.execute_aftersales(
                    account_dict,
                    account.platform,
                    scrape_config=base_scrape_config,
                    on_batch_ready=self._build_aftersale_stream_upload_handler(
                        round_no=round_no,
                        platform=account.platform,
                        shop_account_id=account.id,
                        client_id=self.settings.scraper_client_id,
                    ),
                )
                aftersale_count = len(aftersales)
                logger.info("账号售后抓取成功 account_id=%s aftersales=%s", account.id, aftersale_count)
            except Exception as exc:
                aftersale_error = str(exc)[:500]
                logger.exception("账号售后抓取失败 account_id=%s", account.id)

            run_status = "SUCCESS"
            if order_error and aftersale_error:
                run_status = "FAILED"
            elif order_error or aftersale_error:
                run_status = "PARTIAL"

            error_message = None
            if order_error or aftersale_error:
                error_message = "; ".join(
                    part
                    for part in [
                        f"order_error={order_error}" if order_error else None,
                        f"aftersale_error={aftersale_error}" if aftersale_error else None,
                    ]
                    if part
                )[:500]

            self.log_uploader.upload(
                round_no=round_no,
                platform=account.platform,
                shop_account_id=account.id,
                client_id=self.settings.scraper_client_id,
                run_status=run_status,
                order_count=order_count,
                error_message=error_message,
                log_data={
                    "account_id": account.id,
                    "order_count": order_count,
                    "aftersale_count": aftersale_count,
                    "order_error": order_error,
                    "aftersale_error": aftersale_error,
                },
            )
        except Exception:
            logger.exception("上传运行日志失败 account_id=%s", account.id)

    def execute_task(self, task: dict) -> None:
        """Execute scraping task from backend /task endpoint."""
        account_id = task.get("id")
        round_no = task.get("current_round", 1)
        order_error: str | None = None
        aftersale_error: str | None = None
        order_count = 0
        aftersale_count = 0

        try:
            upload_batch_size = random.randint(20, 50)
            logger.info(
                "任务流式上传配置 batch_size=%s account_id=%s round=%s",
                upload_batch_size,
                account_id,
                round_no,
            )
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
                "aftersale_max_pages": task.get("aftersale_max_pages", task.get("scrape_max_pages", 10)),
            }
            scrape_config = {
                "human_action_min_ms": task.get("human_action_min_ms", 1000),
                "human_action_max_ms": task.get("human_action_max_ms", 5000),
                "scrape_max_pages": task.get("scrape_max_pages", 10),
                "aftersale_max_pages": task.get("aftersale_max_pages", task.get("scrape_max_pages", 10)),
                "max_pages": task.get("scrape_max_pages", 10),
                "upload_batch_size": upload_batch_size,
            }

            try:
                orders, items, price_info = self.executor.execute_account(
                    account_dict,
                    task.get("platform"),
                    scrape_config=scrape_config,
                    on_batch_ready=self._build_stream_upload_handler(
                        round_no=round_no,
                        platform=task.get("platform"),
                        shop_account_id=account_id,
                        client_id=self.settings.scraper_client_id,
                    ),
                )
                order_count = len(orders)
                logger.info("任务订单抓取成功 account_id=%s orders=%s round=%s", account_id, order_count, round_no)
            except Exception as exc:
                order_error = str(exc)[:500]
                logger.exception("任务订单抓取失败，继续执行售后抓取 account_id=%s round=%s", account_id, round_no)

            try:
                aftersales = self.executor.execute_aftersales(
                    account_dict,
                    task.get("platform"),
                    scrape_config=scrape_config,
                    on_batch_ready=self._build_aftersale_stream_upload_handler(
                        round_no=round_no,
                        platform=task.get("platform"),
                        shop_account_id=account_id,
                        client_id=self.settings.scraper_client_id,
                    ),
                )
                aftersale_count = len(aftersales)
                logger.info("任务售后抓取成功 account_id=%s aftersales=%s round=%s", account_id, aftersale_count, round_no)
            except Exception as exc:
                aftersale_error = str(exc)[:500]
                logger.exception("任务售后抓取失败 account_id=%s round=%s", account_id, round_no)

            run_status = "SUCCESS"
            if order_error and aftersale_error:
                run_status = "FAILED"
            elif order_error or aftersale_error:
                run_status = "PARTIAL"

            error_message = None
            if order_error or aftersale_error:
                error_message = "; ".join(
                    part
                    for part in [
                        f"order_error={order_error}" if order_error else None,
                        f"aftersale_error={aftersale_error}" if aftersale_error else None,
                    ]
                    if part
                )[:500]

            self.log_uploader.upload(
                round_no=round_no,
                platform=task.get("platform"),
                shop_account_id=account_id,
                client_id=self.settings.scraper_client_id,
                run_status=run_status,
                order_count=order_count,
                error_message=error_message,
                log_data={
                    "account_id": account_id,
                    "order_count": order_count,
                    "aftersale_count": aftersale_count,
                    "order_error": order_error,
                    "aftersale_error": aftersale_error,
                },
            )
        except Exception:
            logger.exception("上传运行日志失败 account_id=%s", account_id)
