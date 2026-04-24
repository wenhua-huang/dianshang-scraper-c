from __future__ import annotations

from scraper_client.infra.browser.chrome_discovery import ChromeCdpResolver


def test_normalize_candidate_supports_ws_scheme() -> None:
    assert ChromeCdpResolver._normalize_candidate("ws://127.0.0.1:9222/devtools/browser/abc") == [
        "http://127.0.0.1:9222"
    ]


def test_resolve_prefers_playwright_cdp_url_when_ready(monkeypatch) -> None:
    resolver = ChromeCdpResolver(
        preferred_url="http://127.0.0.1:9222",
        timeout_ms=2000,
        client_id="test-client",
    )

    monkeypatch.setattr(resolver, "_discover_local_cdp_urls", lambda: ["http://127.0.0.1:9333"])
    monkeypatch.setattr(
        resolver,
        "_is_cdp_ready",
        lambda url: url == "http://127.0.0.1:9222",
    )

    assert resolver.resolve() == "http://127.0.0.1:9222"


def test_resolve_falls_back_to_discovered_url(monkeypatch) -> None:
    resolver = ChromeCdpResolver(
        preferred_url="http://127.0.0.1:9222",
        timeout_ms=2000,
        client_id="test-client",
    )

    monkeypatch.setattr(resolver, "_discover_local_cdp_urls", lambda: ["http://127.0.0.1:9333"])
    monkeypatch.setattr(
        resolver,
        "_is_cdp_ready",
        lambda url: url == "http://127.0.0.1:9333",
    )

    assert resolver.resolve() == "http://127.0.0.1:9333"


def test_resolve_auto_starts_chrome_when_no_endpoint_ready(monkeypatch) -> None:
    resolver = ChromeCdpResolver(
        preferred_url="http://127.0.0.1:9222",
        timeout_ms=2000,
        client_id="test-client",
    )

    monkeypatch.setattr(resolver, "_discover_local_cdp_urls", lambda: ["http://127.0.0.1:9333"])
    monkeypatch.setattr(resolver, "_is_cdp_ready", lambda _url: False)
    monkeypatch.setattr(resolver, "_launch_chrome_and_wait", lambda: "http://127.0.0.1:9444")

    assert resolver.resolve() == "http://127.0.0.1:9444"
