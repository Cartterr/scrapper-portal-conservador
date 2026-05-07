from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import Settings, find_chrome_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginState:
    logged_in: bool
    token_expires_at: str | None = None


class BrowserSession:
    """Persistent browser session for manual login, cookies, and reCAPTCHA tokens."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._context = None
        self._page = None

    def start(self):
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - dependency check path
            raise RuntimeError("Playwright is not installed. Run: python -m pip install -e .") from exc

        self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": self.settings.headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if find_chrome_path():
            launch_kwargs["channel"] = "chrome"
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.settings.browser_profile_dir),
            bypass_csp=True,
            viewport={"width": 1365, "height": 900},
            **launch_kwargs,
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    @property
    def page(self):
        self.start()
        return self._page

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    def goto_portal(self, path: str = "/") -> None:
        url = f"{self.settings.base_url}{path}"
        logger.info("Opening portal route: %s", path)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self.page.wait_for_timeout(1500)

    def manual_login(self, timeout_seconds: int = 600) -> LoginState:
        """Open the portal and wait for the operator to finish login."""
        self.goto_portal("/usuario/mi-cuenta")
        deadline = datetime.now(UTC).timestamp() + timeout_seconds
        logger.info("Waiting for authenticated browser state")
        while datetime.now(UTC).timestamp() < deadline:
            state = self.login_state()
            if state.logged_in:
                logger.info("Authenticated browser profile is ready")
                return state
            self.page.wait_for_timeout(1000)
        raise TimeoutError("Timed out waiting for CBRS login")

    def login_state(self) -> LoginState:
        token_meta = self.page.evaluate(
            """() => {
                let raw = null;
                try {
                    raw = localStorage.getItem('auth_cbrs_token');
                } catch {
                    return { loggedIn: false, tokenExpiresAt: null };
                }
                if (!raw) return { loggedIn: false, tokenExpiresAt: null };
                try {
                    const parsed = JSON.parse(raw);
                    const token = parsed.token || parsed.accessToken || raw;
                    const payload = JSON.parse(atob(token.split('.')[1]));
                    return {
                        loggedIn: true,
                        tokenExpiresAt: payload.exp ? new Date(payload.exp * 1000).toISOString() : null
                    };
                } catch {
                    return { loggedIn: true, tokenExpiresAt: null };
                }
            }"""
        )
        return LoginState(
            logged_in=bool(token_meta.get("loggedIn")),
            token_expires_at=token_meta.get("tokenExpiresAt"),
        )

    def current_jwt(self) -> str | None:
        return self.page.evaluate(
            """() => {
                let raw = null;
                try {
                    raw = localStorage.getItem('auth_cbrs_token');
                } catch {
                    return null;
                }
                if (!raw) return null;
                try {
                    const parsed = JSON.parse(raw);
                    return parsed.token || parsed.accessToken || raw;
                } catch {
                    return raw;
                }
            }"""
        )

    def cookies(self) -> dict[str, str]:
        self.start()
        return {cookie["name"]: cookie["value"] for cookie in self._context.cookies()}

    def set_cookies(self, cookies: dict[str, str]) -> None:
        self.start()
        self._context.add_cookies(
            [
                {
                    "name": name,
                    "value": value,
                    "domain": "nuevo-portal.conservador.cl",
                    "path": "/",
                    "secure": True,
                }
                for name, value in cookies.items()
            ]
        )

    def ensure_recaptcha_sdk(self) -> None:
        self.goto_portal("/consultas-en-linea/indices/indice-del-registro-de-comercio")
        sdk_ready = self.page.evaluate(
            """() => Boolean(window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute)"""
        )
        if sdk_ready:
            return
        sdk_url = (
            "https://www.google.com/recaptcha/enterprise.js"
            f"?render={self.settings.recaptcha_sitekey}"
        )
        self.page.evaluate(
            """(url) => new Promise((resolve, reject) => {
                const existing = document.querySelector(`script[src="${url}"]`);
                if (existing) return resolve();
                const script = document.createElement('script');
                script.src = url;
                script.onload = resolve;
                script.onerror = reject;
                document.head.appendChild(script);
            })""",
            sdk_url,
        )
        self.page.evaluate(
            """() => new Promise((resolve) => {
                const check = () => {
                    if (window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute) {
                        resolve();
                    } else if (window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.ready) {
                        window.grecaptcha.enterprise.ready(resolve);
                    } else {
                        setTimeout(check, 100);
                    }
                };
                check();
            })"""
        )

    def recaptcha_token(self, action: str) -> str:
        self.ensure_recaptcha_sdk()
        token = self.page.evaluate(
            """async ({ sitekey, action }) => {
                return await grecaptcha.enterprise.execute(sitekey, { action });
            }""",
            {"sitekey": self.settings.recaptcha_sitekey, "action": action},
        )
        logger.info("Generated reCAPTCHA token for action=%s", action)
        return token

    def login_with_credentials(self, email: str, password: str) -> LoginState:
        """Best-effort form login using the portal UI."""
        self.goto_portal("/login")
        self.page.wait_for_selector("#email", timeout=30000)
        self.page.fill("#email", "")
        self.page.type("#email", email, delay=40)
        self.page.fill("#password", "")
        self.page.type("#password", password, delay=40)
        with self.page.expect_response(lambda r: "/api/v1/auth/login" in r.url, timeout=45000):
            self.page.click('button[type=submit]')
        self.page.wait_for_timeout(2000)
        return self.login_state()
