"""JD-specific exceptions."""
from __future__ import annotations


class JDScraperError(RuntimeError):
    """Base exception for all JD scraper errors."""


class JDLoginError(JDScraperError):
    """Raised when login fails (wrong credentials, captcha, 2FA required)."""


class JDSessionExpiredError(JDScraperError):
    """Raised when the browser session / cookie has expired and re-login is needed."""


class JDRateLimitError(JDScraperError):
    """Raised when JD returns 429 / block page responses."""


class JDParseError(JDScraperError):
    """Raised when the response payload cannot be parsed into the expected schema."""
