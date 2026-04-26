from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, Response

from scraper_client.core.logging import configure_logging
from scraper_client.core.settings import get_settings
from scraper_client.infra.jd.authenticator import JDAuthenticator
from scraper_client.infra.jd.session_manager import JDSessionManager

ORDER_LIST_URL = "https://shop.jd.com/jdm/trade/orders/order-list?tabType=allOrders"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe JD order-list page state and network")
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--account-name", type=str, default="probe-account")
    parser.add_argument("--auth", action="store_true", help="Run authenticator before probing")
    parser.add_argument("--manual-login", action="store_true", help="Allow manual login during auth")
    parser.add_argument("--wait-seconds", type=int, default=10, help="Observe network for N seconds")
    return parser


def _collect_doc_diag(target: Page) -> dict[str, Any]:
    return target.evaluate(
        r"""
        () => {
          const body = (document.body && document.body.innerText) ? document.body.innerText : '';
          const pick = (text, n) => (text || '').slice(0, n);
          return {
            title: document.title || '',
            url: location.href || '',
            readyState: document.readyState,
            iframeCount: document.querySelectorAll('iframe').length,
            orderCardCount: document.querySelectorAll('.order-list-card-table .table-body .card').length,
            orderListRootCount: document.querySelectorAll('.order-list-card-table, [class*="order-list"]').length,
            nextButtonCount: document.querySelectorAll('.jd-pagination .btn-next, .jd-pro-pagination .btn-next, button[aria-label="下一页"]').length,
            hasLoginText: body.includes('登录') || body.includes('去登录') || body.includes('账号登录'),
            hasEmptyText: body.includes('暂无数据') || body.includes('暂无订单') || body.includes('无数据'),
            hasNoPermissionText: body.includes('无权限') || body.includes('权限') || body.includes('未开通'),
            bodyPreview: pick(body.replace(/\s+/g, ' '), 300),
          };
        }
        """
    )


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, client_id=f"probe-{settings.scraper_client_id}")
    logger = logging.getLogger("probe_jd_list_page")

    session_manager = JDSessionManager(settings)
    authenticator = JDAuthenticator(session_manager, settings=settings)

    ts = int(time.time())
    out_dir = Path("artifacts") / "debug" / "jd_probe" / f"account_{args.account_id}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    response_hits: list[dict[str, Any]] = []
    response_all: list[dict[str, Any]] = []

    session = session_manager.open_session(args.account_id)
    try:
        account = {
            "id": args.account_id,
            "account_name": args.account_name,
            "account_password": "",
            "phone": "",
            "email": "",
        }

        if args.auth:
            authenticator.ensure_authenticated(
                session,
                account=account,
                allow_manual_login=args.manual_login,
                wait_timeout_seconds=max(60, args.wait_seconds),
            )

        page = session.page

        def _on_response(resp: Response) -> None:
            url = resp.url
            item = {
                "url": url,
                "status": resp.status,
                "content_type": resp.headers.get("content-type", ""),
            }
            if len(response_all) < 300:
                response_all.append(item)
            low = url.lower()
            if any(k in low for k in ["order", "trade", "query", "list", "bff"]):
                if len(response_hits) < 200:
                    response_hits.append(item)

        page.on("response", _on_response)
        page.goto(ORDER_LIST_URL, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(max(1, args.wait_seconds) * 1000)

        main_diag = _collect_doc_diag(page)
        frame_diags: list[dict[str, Any]] = []
        for i, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            try:
                diag = _collect_doc_diag(frame)
                diag["frame_index"] = i
                frame_diags.append(diag)
            except Exception as exc:
                frame_diags.append({"frame_index": i, "error": str(exc), "url": frame.url})

        page.remove_listener("response", _on_response)

        screenshot_path = out_dir / "main.png"
        html_path = out_dir / "main.html"
        page.screenshot(path=str(screenshot_path), full_page=True)
        html_path.write_text(page.content(), encoding="utf-8")

        report = {
            "account_id": args.account_id,
            "order_list_url": ORDER_LIST_URL,
            "current_url": page.url,
            "main_diag": main_diag,
            "frame_diags": frame_diags,
            "response_hits_count": len(response_hits),
            "response_all_count": len(response_all),
            "response_hits": response_hits,
            "artifacts": {
                "dir": str(out_dir),
                "main_png": str(screenshot_path),
                "main_html": str(html_path),
            },
        }
        report_path = out_dir / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps(report, ensure_ascii=False, indent=2))
        logger.info("probe report saved path=%s", report_path)
        return 0
    finally:
        session_manager.close_session(session, persist_state=True)


if __name__ == "__main__":
    raise SystemExit(main())
