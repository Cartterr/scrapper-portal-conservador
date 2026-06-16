"""Persistent session management for CBRS login bypass.

When automated login fails (Imperva WAF + reCAPTCHA blocks Playwright),
users can log in manually via `cbrs init` and save the session. The saved
refresh token is then used by curl_cffi to obtain new JWTs without needing
to go through the browser-based login flow.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SESSION_FILE = Path(__file__).parent.parent / ".cbrs_session.json"


def save_session(cookies: dict[str, str]) -> Path:
    """Save browser cookies (including refresh token) to disk."""
    SESSION_FILE.write_text(json.dumps(cookies, indent=2))
    logger.info("Session saved to %s", SESSION_FILE)
    return SESSION_FILE


def load_session() -> dict[str, str] | None:
    """Load saved session cookies, or None if no session exists."""
    if not SESSION_FILE.exists():
        return None
    try:
        cookies = json.loads(SESSION_FILE.read_text())
        if "cbrs_refresh_token" not in cookies:
            logger.warning("Saved session has no refresh token, ignoring")
            return None
        return cookies
    except Exception as e:
        logger.warning("Failed to load session: %s", e)
        return None


def clear_session():
    """Delete the saved session file."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        logger.info("Session cleared")
