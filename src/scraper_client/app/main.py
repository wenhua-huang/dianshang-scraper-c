from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from scraper_client.core.logging import configure_logging
from scraper_client.core.settings import get_resolved_env_file, get_settings
from scraper_client.core.settings import Settings
from scraper_client.services.account_orchestrator import AccountOrchestrator

logger = logging.getLogger(__name__)

ENV_PROFILES: dict[str, dict[str, str]] = {
    "test": {
        "scraper_server_base_url": "http://111.228.37.167:8000/api/v1",
        "scraper_internal_api_key": "change-me-scraper-key",
        "scraper_client_id": "scraper-client-test",
    },
    "prod": {
        "scraper_server_base_url": "https://your-backend-host/api/v1",
        "scraper_internal_api_key": "change-me-scraper-key",
        "scraper_client_id": "scraper-client-prod",
    },
}

DEFAULT_SENTINELS: dict[str, str] = {
    "scraper_server_base_url": "http://127.0.0.1:8000/api/v1",
    "scraper_internal_api_key": "change-me-scraper-key",
    "scraper_client_id": "scraper-client-local",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="dianshang scraper client")
    sub = parser.add_subparsers(dest="command")

    start = sub.add_parser("start", help="Run continuously and execute active accounts")
    start.add_argument("--poll-interval", type=int, default=None, help="Override poll interval seconds")
    start.add_argument("--empty-backoff", type=int, default=None, help="Override empty queue backoff seconds")

    start_test = sub.add_parser("start-test", help="Run with built-in TEST defaults")
    start_test.add_argument("--poll-interval", type=int, default=None, help="Override poll interval seconds")
    start_test.add_argument("--empty-backoff", type=int, default=None, help="Override empty queue backoff seconds")

    start_prod = sub.add_parser("start-prod", help="Run with built-in PROD defaults")
    start_prod.add_argument("--poll-interval", type=int, default=None, help="Override poll interval seconds")
    start_prod.add_argument("--empty-backoff", type=int, default=None, help="Override empty queue backoff seconds")

    return parser


def _prepare_argv(argv: list[str] | None) -> tuple[list[str], bool]:
    if argv is not None:
        return argv, False
    if len(sys.argv) > 1:
        return sys.argv[1:], False
    # Running executable by double-click often provides no args. Default to daemon mode.
    return ["start"], True


def _infer_profile_from_command(command: str | None) -> str | None:
    if command == "start-test":
        return "test"
    if command == "start-prod":
        return "prod"
    return None


def _infer_profile_from_executable() -> str | None:
    exe_name = Path(sys.argv[0]).name.lower()
    if "-test" in exe_name or "_test" in exe_name:
        return "test"
    if "-prod" in exe_name or "_prod" in exe_name:
        return "prod"
    return None


def _apply_profile_defaults(settings: Settings, profile: str | None) -> None:
    if profile is None:
        return
    profile_values = ENV_PROFILES.get(profile)
    if profile_values is None:
        return

    for field, profile_value in profile_values.items():
        current_value = getattr(settings, field)
        if str(current_value) == DEFAULT_SENTINELS.get(field):
            setattr(settings, field, profile_value)


def _is_using_default_core_config(settings: Settings) -> bool:
    return (
        settings.scraper_server_base_url == DEFAULT_SENTINELS["scraper_server_base_url"]
        and settings.scraper_internal_api_key == DEFAULT_SENTINELS["scraper_internal_api_key"]
        and settings.scraper_client_id == DEFAULT_SENTINELS["scraper_client_id"]
    )


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

    env_file_path = Path("<unknown>")
    active_profile = None
    try:
        env_file_path = get_resolved_env_file()
        settings = get_settings()
        active_profile = _infer_profile_from_command(args.command) or _infer_profile_from_executable()
        _apply_profile_defaults(settings, active_profile)

        # Hard fallback: when no env file exists and no profile is inferred,
        # avoid localhost/default placeholders by switching to TEST profile.
        if (
            active_profile is None
            and args.command == "start"
            and not env_file_path.exists()
            and _is_using_default_core_config(settings)
        ):
            active_profile = "test"
            _apply_profile_defaults(settings, active_profile)

        if args.command in {"start", "start-test", "start-prod"}:
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
    if active_profile is not None:
        logger.info("active profile: %s", active_profile)
    logger.info("backend base url: %s", settings.scraper_server_base_url)
    logger.info("env file: %s exists=%s", env_file_path, env_file_path.exists())
    if log_path is not None:
        logger.info("log file: %s", log_path)
        print(f"[scraper-client] running, log file: {log_path}", flush=True)
    else:
        print("[scraper-client] running", flush=True)

    try:
        if args.command in {"start", "start-test", "start-prod"}:
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
