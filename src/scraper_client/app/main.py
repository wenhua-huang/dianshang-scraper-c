from __future__ import annotations

import argparse
import logging
import sys

from scraper_client.core.logging import configure_logging
from scraper_client.core.settings import get_settings
from scraper_client.services.account_orchestrator import AccountOrchestrator

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="dianshang scraper client")
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="Run continuously and execute active accounts")
    start.add_argument("--poll-interval", type=int, default=None, help="Override poll interval seconds")
    start.add_argument("--empty-backoff", type=int, default=None, help="Override empty queue backoff seconds")

    return parser


def _prepare_argv(argv: list[str] | None) -> tuple[list[str], bool]:
    if argv is not None:
        return argv, False
    if len(sys.argv) > 1:
        return sys.argv[1:], False
    # Running executable by double-click often provides no args. Default to daemon mode.
    return ["start"], True


def _pause_before_exit() -> None:
    try:
        input("\n启动失败，请按回车键退出...\n")
    except EOFError:
        return


def main(argv: list[str] | None = None) -> int:
    normalized_argv, auto_started = _prepare_argv(argv)
    args = build_parser().parse_args(normalized_argv)
    if args.command is None:
        args = build_parser().parse_args(["start"])
        auto_started = True

    try:
        settings = get_settings()
        if args.command == "start":
            if args.poll_interval is not None:
                settings.poll_interval_seconds = max(1, args.poll_interval)
            if args.empty_backoff is not None:
                settings.empty_queue_backoff_seconds = max(1, args.empty_backoff)
        settings.validate()
        log_path = configure_logging(settings.log_level, client_id=settings.scraper_client_id)
    except Exception as exc:
        print(f"[scraper-client] startup failed: {exc}")
        if auto_started:
            _pause_before_exit()
        return 1

    logger.info("scraper-client starting command=%s client_id=%s", args.command, settings.scraper_client_id)
    if auto_started:
        logger.info("detected no CLI args, defaulted to 'start' mode")
    logger.info("backend base url: %s", settings.scraper_server_base_url)
    if log_path is not None:
        logger.info("log file: %s", log_path)
        print(f"[scraper-client] running, log file: {log_path}", flush=True)
    else:
        print("[scraper-client] running", flush=True)

    try:
        if args.command == "start":
            orchestrator = AccountOrchestrator(settings)
            orchestrator.register_signal_handlers()
            orchestrator.run_forever()
            return 0
    except Exception:
        logger.exception("fatal error, scraper-client is exiting")
        if auto_started:
            _pause_before_exit()
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
