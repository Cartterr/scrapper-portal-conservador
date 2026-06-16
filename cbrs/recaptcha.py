"""Playwright wrapper for reCAPTCHA Enterprise tokens and browser-based login.

The browser handles reCAPTCHA token generation and login. Two login methods:
- Form-based (default): interacts with the Vue.js login form naturally. This
  bypasses Imperva WAF automation detection that blocks direct fetch() calls.
- Fetch-based (fallback): calls /api/v1/auth/login via fetch() in the browser.
  Faster but may be blocked by WAF when IP is flagged.
After login, the JWT is extracted and used by curl_cffi for subsequent API calls.
"""

import logging
import shutil

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

from . import config

logger = logging.getLogger(__name__)

_RECAPTCHA_SDK_URL = (
    "https://www.google.com/recaptcha/enterprise.js"
    f"?render={config.RECAPTCHA_SITEKEY}"
)


class RecaptchaTokenGenerator:
    """Generates real reCAPTCHA Enterprise tokens using a browser.

    Uses Playwright to run a minimal Chrome instance that loads the
    reCAPTCHA Enterprise SDK and executes grecaptcha.enterprise.execute()
    to produce valid tokens with high scores.
    """

    def __init__(self, headless: bool = True, proxy: dict | None = None):
        self._proxy = proxy
        self._pw = sync_playwright().start()

        # Prefer system Chrome (better fingerprint for reCAPTCHA scoring),
        # fall back to bundled Chromium for headless servers without Chrome.
        use_chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
        launch_kwargs = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if use_chrome:
            launch_kwargs["channel"] = "chrome"
            logger.debug("Using system Chrome for reCAPTCHA")
        else:
            logger.info(
                "System Chrome not found, using bundled Chromium. "
                "Run 'playwright install chromium' if not already installed."
            )
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info("Browser using proxy: %s", proxy.get("server", "?"))

        self._browser: Browser = self._pw.chromium.launch(**launch_kwargs)

        # In headless mode, Chrome's UA contains "HeadlessChrome" which
        # reCAPTCHA Enterprise detects and assigns low scores. We fix the
        # UA to say "Chrome" instead. navigator.userAgentData is unaffected
        # (it never contains "Headless"), so there's no mismatch.
        context_kwargs: dict = {"bypass_csp": True}
        if headless:
            tmp_ctx = self._browser.new_context()
            tmp_page = tmp_ctx.new_page()
            default_ua = tmp_page.evaluate("navigator.userAgent")
            tmp_ctx.close()
            if "HeadlessChrome" in default_ua:
                patched_ua = default_ua.replace("HeadlessChrome", "Chrome")
                context_kwargs["user_agent"] = patched_ua
                logger.debug("Patched headless UA: %s", patched_ua)

        self._context: BrowserContext = self._browser.new_context(
            **context_kwargs,
        )

        # Apply stealth patches to bypass Imperva Advanced Bot Protection.
        # Falls back to basic webdriver hiding if playwright-stealth not installed.
        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(self._context)
            logger.debug("Applied playwright-stealth patches")
        except ImportError:
            self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{ get: () => undefined });"
            )
        self._page: Page = self._context.new_page()
        self._initialized = False

    def _init(self):
        """Navigate to the site and inject reCAPTCHA Enterprise SDK."""
        if self._initialized:
            return

        # Verify proxy IP if proxy is active
        if self._proxy:
            try:
                self._page.goto("https://api.ipify.org?format=json", timeout=15000)
                ip_data = self._page.evaluate("document.body.innerText")
                logger.info("Browser external IP: %s", ip_data)
            except Exception as e:
                logger.warning("Could not verify proxy IP: %s", e)
            # Navigate away before loading the real page
            self._page.goto("about:blank")

        logger.info("Loading commerce index page for reCAPTCHA SDK...")
        self._page.goto(
            f"{config.BASE_URL}"
            "/consultas-en-linea/indices/indice-del-registro-de-comercio",
            wait_until="networkidle",
            timeout=30000,
        )

        logger.info("Injecting reCAPTCHA Enterprise SDK...")
        self._page.evaluate(
            """(url) => {
                return new Promise((resolve, reject) => {
                    const script = document.createElement('script');
                    script.src = url;
                    script.onload = resolve;
                    script.onerror = reject;
                    document.head.appendChild(script);
                });
            }""",
            _RECAPTCHA_SDK_URL,
        )

        self._page.evaluate(
            """() => {
                return new Promise((resolve) => {
                    const check = () => {
                        if (typeof grecaptcha !== 'undefined'
                            && grecaptcha.enterprise
                            && grecaptcha.enterprise.execute) {
                            resolve();
                        } else if (typeof grecaptcha !== 'undefined'
                                   && grecaptcha.enterprise
                                   && grecaptcha.enterprise.ready) {
                            grecaptcha.enterprise.ready(resolve);
                        } else {
                            setTimeout(check, 100);
                        }
                    };
                    check();
                });
            }"""
        )
        self._initialized = True
        logger.info("reCAPTCHA Enterprise SDK ready")

    def generate_token(self, action: str) -> str:
        """Generate a reCAPTCHA Enterprise token for the given action."""
        self._init()
        logger.info("Generating reCAPTCHA token (action=%s)...", action)
        token = self._page.evaluate(
            """async (args) => {
                return await grecaptcha.enterprise.execute(
                    args.sitekey, { action: args.action }
                );
            }""",
            {"sitekey": config.RECAPTCHA_SITEKEY, "action": action},
        )
        logger.info("reCAPTCHA token generated (%d chars)", len(token))
        return token

    def login(self, email: str, password: str) -> dict:
        """Login using form interaction (default), fall back to fetch on error.

        Form-based login interacts with the Vue.js app's native login form,
        which bypasses Imperva WAF automation detection. Falls back to the
        faster fetch-based approach if form interaction fails.
        """
        try:
            return self._login_form(email, password)
        except Exception as exc:
            logger.warning("Form login failed (%s), trying fetch login...", exc)
            self._initialized = False
            return self._login_fetch(email, password)

    def _login_form(self, email: str, password: str) -> dict:
        """Login by interacting with the Vue.js login form."""
        self._init()

        # Open login form if not already visible
        logger.info("Opening login form...")
        if not self._page.is_visible("#email"):
            self._page.click('button:has-text("Iniciar sesión")')
            self._page.wait_for_selector("#email", state="visible", timeout=5000)

        # Fill credentials
        logger.info("Logging in as %s (form)...", email)
        self._page.fill("#email", "")
        self._page.type("#email", email, delay=50)
        self._page.fill("#password", "")
        self._page.type("#password", password, delay=50)

        # Submit and capture the login API response
        with self._page.expect_response(
            lambda r: "/api/v1/auth/login" in r.url, timeout=30000,
        ) as response_info:
            self._page.click('button[type=submit]:has-text("Iniciar")')

        response = response_info.value
        data = response.json()

        # Reset page state on failure so next attempt reinitializes
        if response.status != 200:
            self._initialized = False

        return {"status": response.status, "data": data}

    def _login_fetch(self, email: str, password: str) -> dict:
        """Login via fetch() inside the browser (legacy approach)."""
        self._init()

        # Call /home/start from the browser
        logger.info("Getting session hint (browser)...")
        self._page.evaluate(
            """async () => {
                await fetch('/api/v1/home/start', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ preHint: '' })
                });
            }"""
        )

        # Generate reCAPTCHA token for login
        captcha_token = self.generate_token("login")

        # Login via fetch inside the browser
        logger.info("Logging in as %s (fetch)...", email)
        result = self._page.evaluate(
            """async (args) => {
                const resp = await fetch('/api/v1/auth/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'recaptcha-token': args.token
                    },
                    body: JSON.stringify({
                        email: args.email,
                        password: args.password
                    })
                });
                return { status: resp.status, data: await resp.json() };
            }""",
            {"email": email, "password": password, "token": captcha_token},
        )

        return result

    def set_cookies(self, cookies: dict[str, str], domain: str):
        """Inject cookies into the browser context before navigation."""
        self._context.add_cookies([
            {"name": name, "value": value, "domain": domain, "path": "/"}
            for name, value in cookies.items()
        ])

    def get_cookies(self) -> dict[str, str]:
        """Return all cookies from the browser context as a dict."""
        self._init()
        cookies = self._context.cookies()
        return {c["name"]: c["value"] for c in cookies}

    def close(self):
        self._browser.close()
        self._pw.stop()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
