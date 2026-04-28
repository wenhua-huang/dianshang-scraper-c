from __future__ import annotations

import atexit
import json
import logging
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class ChromeDiscoveryError(RuntimeError):
    """Raised when CDP endpoint cannot be discovered or started."""


class ChromeCdpResolver:
    def __init__(self, *, preferred_url: str, timeout_ms: int, client_id: str) -> None:
        self._preferred_url = preferred_url
        self._timeout_ms = max(1_000, int(timeout_ms))
        safe_client_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in client_id)
        self._profile_dir = Path("artifacts") / "chrome-profile" / safe_client_id
        self._resolved_url: str | None = None
        self._started_process: subprocess.Popen[bytes] | None = None

    def resolve(self) -> str:
        if self._resolved_url and self._is_cdp_ready(self._resolved_url):
            return self._resolved_url

        urls_to_try = self._candidate_urls()
        for url in urls_to_try:
            if self._is_cdp_ready(url):
                self._resolved_url = url
                logger.info("CDP endpoint resolved: %s", url)
                return url

        launched_url = self._launch_chrome_and_wait()
        self._resolved_url = launched_url
        return launched_url

    def _candidate_urls(self) -> list[str]:
        candidates: list[str] = []
        candidates.extend(self._normalize_candidate(self._preferred_url))
        candidates.extend(self._discover_local_cdp_urls())

        seen: set[str] = set()
        uniq: list[str] = []
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            uniq.append(item)
        return uniq

    @staticmethod
    def _normalize_candidate(url: str | None) -> list[str]:
        if not url:
            return []
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return []

        if parsed.scheme in {"ws", "wss"}:
            scheme = "http" if parsed.scheme == "ws" else "https"
            return [f"{scheme}://{parsed.netloc}"]

        if parsed.scheme in {"http", "https"}:
            return [f"{parsed.scheme}://{parsed.netloc}"]

        return []

    def _discover_local_cdp_urls(self) -> list[str]:
        # Scan common local debugging ports to discover already running Chrome CDP.
        ports = [9222, 9223, 9224, 9225, 9333]
        return [f"http://127.0.0.1:{port}" for port in ports]

    def _is_cdp_ready(self, base_url: str) -> bool:
        version_url = f"{base_url.rstrip('/')}/json/version"
        request = Request(version_url, headers={"User-Agent": "scraper-client"})
        try:
            with urlopen(request, timeout=max(1.0, self._timeout_ms / 1000)) as response:
                if response.status != 200:
                    return False
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        except Exception:
            return False

        websocket_url = payload.get("webSocketDebuggerUrl")
        browser_name = str(payload.get("Browser", ""))
        return bool(websocket_url) or "Chrome" in browser_name or "Chromium" in browser_name

    def _launch_chrome_and_wait(self) -> str:
        executable = self._find_chrome_executable()
        if not executable:
            raise ChromeDiscoveryError(
                "Cannot find Chrome/Chromium executable for CDP startup"
            )

        port = self._preferred_port() or self._find_free_port(9222)
        cdp_url = f"http://127.0.0.1:{port}"
        self._profile_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            executable,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self._profile_dir.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )

        logger.warning(
            "PLAYWRIGHT_CDP_URL unavailable, starting Chrome automatically executable=%s port=%s",
            executable,
            port,
        )

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:
            raise ChromeDiscoveryError(f"Failed to start Chrome for CDP: {exc}") from exc

        self._started_process = proc
        atexit.register(self._terminate_started_chrome)

        deadline = time.time() + max(10.0, self._timeout_ms / 1000)
        while time.time() < deadline:
            if self._is_cdp_ready(cdp_url):
                logger.info("Chrome CDP started successfully at %s", cdp_url)
                return cdp_url
            if proc.poll() is not None:
                raise ChromeDiscoveryError("Chrome exited before CDP endpoint became ready")
            sleep_secs = 0.5
            logger.debug("Chrome CDP not ready yet, sleeping_secs=%s", sleep_secs)
            time.sleep(sleep_secs)

        raise ChromeDiscoveryError("Timed out waiting for auto-started Chrome CDP endpoint")

    def _terminate_started_chrome(self) -> None:
        proc = self._started_process
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _preferred_port(self) -> int | None:
        candidates = self._normalize_candidate(self._preferred_url)
        if not candidates:
            return None
        parsed = urlparse(candidates[0])
        return parsed.port

    @staticmethod
    def _find_free_port(preferred: int) -> int:
        for candidate in [preferred, 9222, 9223, 9333]:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", candidate))
                except OSError:
                    continue
                return candidate

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _find_chrome_executable() -> str | None:
        path_candidates: list[str] = []
        if sys.platform == "darwin":
            path_candidates.extend(
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                ]
            )
        elif sys.platform.startswith("win"):
            path_candidates.extend(
                [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                ]
            )

        for path in path_candidates:
            if Path(path).exists():
                return path

        for command in [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "chrome",
            "chrome.exe",
        ]:
            found = shutil.which(command)
            if found:
                return found

        return None
