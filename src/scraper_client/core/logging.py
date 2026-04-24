from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(level: str = "INFO", *, client_id: str = "scraper-client") -> Path | None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    log_format = "%(asctime)s %(levelname)s %(name)s - %(message)s"

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file_path: Path | None = None

    try:
        log_dir = Path.cwd() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_client_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in client_id)
        safe_client_id = safe_client_id.strip("_") or "scraper-client"
        log_file_path = log_dir / f"{safe_client_id}.log"
        handlers.append(
            RotatingFileHandler(
                filename=log_file_path,
                maxBytes=2 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    except Exception:
        # Keep stdout logging available even if file logging cannot be initialized.
        log_file_path = None

    logging.basicConfig(level=log_level, format=log_format, handlers=handlers)
    return log_file_path
