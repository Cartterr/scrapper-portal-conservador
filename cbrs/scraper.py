"""CBRS Commerce Registry scraper using curl_cffi + Playwright for reCAPTCHA.

Uses curl_cffi (with Chrome TLS fingerprint impersonation) for all HTTP
requests, and a minimal Playwright browser instance solely for generating
reCAPTCHA Enterprise tokens.

Advantages over the previous pure-Playwright approach:
- Image downloads are direct binary (no base64 round-trip)
- API calls are faster (native HTTP vs browser JS fetch)
- Less browser resource usage
- Easier debugging (see actual HTTP request/response)
- Browser only does one thing: generate reCAPTCHA tokens
"""

import logging
import re
from pathlib import Path

from curl_cffi.requests import Session
from PIL import Image

from . import config
from .recaptcha import RecaptchaTokenGenerator
from .session import load_session, save_session

logger = logging.getLogger(__name__)

_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": config.BASE_URL,
    "Referer": (
        f"{config.BASE_URL}"
        "/consultas-en-linea/indices/indice-del-registro-de-comercio"
    ),
    "sec-ch-ua": (
        f'"Not_A Brand";v="8", "Chromium";v="{config.CHROME_MAJOR}", '
        f'"Google Chrome";v="{config.CHROME_MAJOR}"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def create_pdf(image_paths: list[Path], pdf_path: Path) -> Path:
    """Create a PDF from a list of JPEG image paths.

    Sorts by page number extracted from filename to guarantee correct order.
    """
    # Sort by page number: filenames are like {foja}_{numero}_{ano}_page{N}.jpg
    def page_sort_key(p: Path) -> int:
        m = re.search(r"_page(\d+)\.", p.name)
        return int(m.group(1)) if m else 0

    sorted_paths = sorted(image_paths, key=page_sort_key)

    images = []
    for p in sorted_paths:
        img = Image.open(p)
        images.append(img.convert("RGB"))

    if not images:
        raise ValueError("No images to assemble into PDF")

    first, *rest = images
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    first.save(pdf_path, "PDF", save_all=True, append_images=rest)
    logger.info("Created PDF with %d page(s): %s", len(images), pdf_path)
    return pdf_path


class CBRSScraper:
    """Scraper for the CBRS Commerce Registry portal.

    Uses curl_cffi for HTTP requests (Chrome TLS fingerprint) and a minimal
    Playwright browser for reCAPTCHA Enterprise token generation.
    """

    def __init__(self, headless: bool = True, accounts: list[dict] | None = None,
                 use_proxy: bool = False):
        self._accounts = accounts or config.ACCOUNTS
        if not self._accounts:
            raise RuntimeError(
                "No CBRS accounts configured. "
                "Set USER/PASSWORD or USER_1/PASSWORD_1 in .env"
            )
        self._account_index = 0
        self._exhausted_accounts: set[int] = set()

        # Load proxy config if requested (only affects browser login, not curl_cffi)
        proxy = None
        if use_proxy:
            proxy = config.get_proxy_config()
            if not proxy:
                raise RuntimeError(
                    "--use-proxy requires PROXY_2CAPTCHA_HOST in .env "
                    "(also: PROXY_2CAPTCHA_PORT, PROXY_2CAPTCHA_USER, PROXY_2CAPTCHA_PASS)"
                )

        # Playwright browser — only for reCAPTCHA tokens + WAF cookies
        self._recaptcha = RecaptchaTokenGenerator(headless=headless, proxy=proxy)

        # curl_cffi session — for all HTTP requests
        self._session = Session(impersonate=config.CURL_CFFI_IMPERSONATE)

        self._jwt: str | None = None
        self._logged_in = False

    def close(self):
        self._recaptcha.close()
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _rotate_account(self):
        """Switch to the next available account after hitting the daily limit."""
        self._exhausted_accounts.add(self._account_index)

        if len(self._exhausted_accounts) >= len(self._accounts):
            raise RuntimeError(
                "All CBRS accounts have hit the daily query limit. "
                "Add more accounts to .env or try again tomorrow."
            )

        self._account_index = (self._account_index + 1) % len(self._accounts)
        while self._account_index in self._exhausted_accounts:
            self._account_index = (self._account_index + 1) % len(self._accounts)

        self._logged_in = False
        self._jwt = None

        next_email = self._accounts[self._account_index]["email"]
        logger.warning("Rotating to account: %s", next_email)

    def _sync_cookies(self):
        """Transfer WAF cookies from the browser to the curl_cffi session."""
        browser_cookies = self._recaptcha.get_cookies()
        for name, value in browser_cookies.items():
            self._session.cookies.set(
                name, value, domain="nuevo-portal.conservador.cl"
            )
        logger.debug("Synced %d cookies from browser", len(browser_cookies))

    def _post_json(self, path: str, body: dict, headers: dict | None = None) -> dict:
        """POST JSON to the API and return {status, data}."""
        hdrs = {**_API_HEADERS, **(headers or {})}
        resp = self._session.post(
            f"{config.BASE_URL}{path}",
            headers=hdrs,
            json=body,
        )
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        return {"status": resp.status_code, "data": data}

    def _login_from_session(self) -> bool:
        """Try to log in using a saved session (refresh token). Returns True on success."""
        saved = load_session()
        if not saved:
            return False

        logger.info("Found saved session, attempting token refresh...")

        # Load saved cookies into BOTH curl_cffi and Playwright browser.
        # curl_cffi needs them for API calls (WAF trusts browser-originated cookies).
        # Playwright needs them so reCAPTCHA tokens are generated from a trusted
        # session context (improves score).
        for name, value in saved.items():
            self._session.cookies.set(
                name, value, domain="nuevo-portal.conservador.cl"
            )
        self._recaptcha.set_cookies(saved, "nuevo-portal.conservador.cl")

        # Try to refresh
        result = self._post_json("/api/v1/auth/refresh", {})
        if result["status"] == 200 and isinstance(result["data"], dict):
            token = result["data"].get("token")
            if token:
                self._jwt = token
                self._session.cookies.set(
                    "auth_cbrs_token",
                    f'"{self._jwt}"',
                    domain="nuevo-portal.conservador.cl",
                )
                # Save the rotated refresh token (server revokes old one)
                new_refresh = self._session.cookies.get(
                    "cbrs_refresh_token", domain="nuevo-portal.conservador.cl"
                )
                if new_refresh:
                    saved["cbrs_refresh_token"] = new_refresh
                    save_session(saved)
                logger.info("Login via saved session successful")
                return True

        logger.warning("Saved session expired or invalid")
        return False

    def login(self):
        """Authenticate with the CBRS portal.

        Tries three approaches in order:
        1. Saved session (refresh token from `cbrs init`)
        2. Form-based browser login (via Playwright)
        3. Fetch-based browser login (legacy fallback)
        """
        if self._logged_in:
            return

        # Try saved session first (no browser needed)
        if self._login_from_session():
            self._logged_in = True
            return

        # Fall back to browser-based login
        account = self._accounts[self._account_index]
        result = self._recaptcha.login(account["email"], account["password"])

        if result["status"] != 200:
            raise RuntimeError(
                f"Login failed: {result['status']} {result.get('data')}"
            )

        self._jwt = result["data"]["token"]
        self._logged_in = True

        # Sync browser cookies (WAF + auth) to curl_cffi for API calls
        self._sync_cookies()

        # Set auth cookie for image downloads
        self._session.cookies.set(
            "auth_cbrs_token",
            f'"{self._jwt}"',
            domain="nuevo-portal.conservador.cl",
        )

        logger.info("Login successful")

    def _ensure_logged_in(self):
        """Ensure we're logged in, refreshing token if needed."""
        if not self._logged_in:
            self.login()
            return

        # Try to refresh the JWT
        result = self._post_json("/api/v1/auth/refresh", {})

        if result["status"] == 200 and isinstance(result["data"], dict):
            self._jwt = result["data"].get("token", self._jwt)
            self._session.cookies.set(
                "auth_cbrs_token",
                f'"{self._jwt}"',
                domain="nuevo-portal.conservador.cl",
            )
        else:
            logger.warning("Token refresh failed, re-logging in...")
            self._logged_in = False
            self.login()

    def _search(self, body: dict) -> list[dict]:
        """Execute a search request against the commerce index.

        Automatically rotates to the next account if the daily query limit
        is reached (err-limite).
        """
        self._ensure_logged_in()
        captcha_token = self._recaptcha.generate_token("indice_com_texto")
        self._sync_cookies()  # ensure curl_cffi has fresh WAF cookies from browser
        body["recaptchaToken"] = captcha_token

        result = self._post_json(
            "/api/v1/comercio/indice/texto",
            body,
            headers={
                "Authorization": f"Bearer {self._jwt}",
                "recaptcha-token": captcha_token,
            },
        )

        # Detect daily query limit exhaustion
        data = result.get("data", {})
        if (
            result["status"] == 400
            and isinstance(data, dict)
            and data.get("code") == "err-limite"
        ):
            account_email = self._accounts[self._account_index]["email"]
            logger.warning(
                "Account %s hit daily query limit. Rotating...", account_email
            )
            self._rotate_account()
            return self._search(body)

        if result["status"] != 200:
            raise RuntimeError(
                f"Search failed: {result['status']} {result.get('data')}"
            )

        return result["data"]

    def search_by_text(self, texto: str) -> list[dict]:
        """Search commerce inscriptions by razón social / text."""
        logger.info("Searching by text: %s", texto)
        body = {
            "foja": None,
            "numero": None,
            "ano": None,
            "texto": texto,
            "recaptchaToken": None,
            "ticket": None,
            "titulosAnteriores": False,
            "comuna": None,
            "anoP": None,
            "origen": "texto",
        }
        return self._search(body)

    def search_by_fna(self, foja: int, numero: int, ano: int) -> list[dict]:
        """Search commerce inscriptions by foja/número/año."""
        logger.info(
            "Searching by FNA: foja=%d, numero=%d, ano=%d", foja, numero, ano
        )
        body = {
            "foja": foja,
            "numero": numero,
            "ano": ano,
            "texto": None,
            "recaptchaToken": None,
            "ticket": None,
            "titulosAnteriores": False,
            "comuna": None,
            "anoP": None,
            "origen": "fna",
        }
        return self._search(body)

    def get_image_refs(self, ticket: str) -> tuple[dict, list[dict]]:
        """Validate ticket and get image references."""
        self._ensure_logged_in()
        captcha_token = self._recaptcha.generate_token("indice_com_texto")
        self._sync_cookies()  # ensure curl_cffi has fresh WAF cookies from browser

        # Validate ticket
        logger.info("Validating ticket: %s...", ticket[:20])
        ticket_result = self._post_json(
            "/api/v1/comercio/indice/fnaTicket",
            {"ticket": ticket},
            headers={
                "Authorization": f"Bearer {self._jwt}",
                "recaptcha-token": captcha_token,
            },
        )

        if ticket_result["status"] != 200:
            raise RuntimeError(
                f"Ticket validation failed: {ticket_result['status']} "
                f"{ticket_result.get('data')}"
            )

        ticket_info = ticket_result["data"]

        # Get image refs
        logger.info(
            "Getting image refs for foja=%s, numero=%s, ano=%s",
            ticket_info.get("foja"),
            ticket_info.get("numero"),
            ticket_info.get("ano"),
        )
        refs_result = self._post_json(
            "/api/v1/comercio/indice/img",
            ticket_info,
            headers={"Authorization": f"Bearer {self._jwt}"},
        )

        if refs_result["status"] != 200:
            raise RuntimeError(
                f"Image refs failed: {refs_result['status']} "
                f"{refs_result.get('data')}"
            )

        refs = refs_result["data"].get("refs", [])
        logger.info("Found %d page(s)", len(refs))
        return ticket_info, refs

    def download_image(self, uuid: str, output_path: Path) -> Path:
        """Download a single image by UUID and save to output_path.

        Downloads directly as binary — no base64 round-trip needed.
        """
        logger.info("Downloading image %s -> %s", uuid[:12] + "...", output_path)

        resp = self._session.get(
            f"{config.BASE_URL}/api/v1/comercio/indice/img/{uuid}",
            headers={
                "Accept": "image/jpeg, image/*, */*",
                "Referer": (
                    f"{config.BASE_URL}"
                    "/consultas-en-linea/indices"
                    "/indice-del-registro-de-comercio"
                ),
            },
        )

        if resp.status_code != 200:
            raise RuntimeError(
                f"Image download failed: {resp.status_code} for {uuid}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(resp.content)
        logger.info("Saved %d bytes to %s", len(resp.content), output_path)
        return output_path

    def download_all_images(
        self, ticket: str, output_dir: Path, *, keep_images: bool = False
    ) -> Path:
        """Download all images for a ticket and assemble them into a PDF.

        Returns the path to the generated PDF file.
        If keep_images is True, the individual JPEG files are kept alongside the PDF.
        """
        ticket_info, refs = self.get_image_refs(ticket)

        foja = ticket_info.get("foja", "unknown")
        numero = ticket_info.get("numero", "unknown")
        ano = ticket_info.get("ano", "unknown")

        downloaded = []
        for ref in refs:
            page_num = ref["pageNumber"]
            uuid = ref["dataRef"]
            filename = f"{foja}_{numero}_{ano}_page{page_num}.jpg"
            output_path = output_dir / filename
            self.download_image(uuid, output_path)
            downloaded.append(output_path)

        pdf_path = output_dir / f"{foja}_{numero}_{ano}.pdf"
        create_pdf(downloaded, pdf_path)

        if not keep_images:
            for p in downloaded:
                p.unlink()
            logger.info("Deleted %d image file(s)", len(downloaded))

        return pdf_path
