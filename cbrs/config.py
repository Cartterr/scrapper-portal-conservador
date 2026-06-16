import os
import re
import shutil
import subprocess

from dotenv import dotenv_values

# Load .env with override so USER isn't shadowed by system env
_env = dotenv_values()

BASE_URL = "https://nuevo-portal.conservador.cl"

# reCAPTCHA Enterprise
RECAPTCHA_SITEKEY = "6Le-eiksAAAAANU-0ITcjxvGfFoHsz40juvUVI_-"

# CapSolver (optional — for HTTP-only scraping experiments)
CAPSOLVER_API_KEY = _env.get("CAPSOLVER_API_KEY") or os.getenv("CAPSOLVER_API_KEY")


# --- Chrome version detection ---
# Detect the real Chrome version so Playwright UA, curl_cffi impersonation,
# and sec-ch-ua headers all stay consistent with navigator.userAgentData.

def _detect_chrome_major() -> int:
    """Return the major version of the system Chrome, or 120 as fallback."""
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if chrome:
        try:
            out = subprocess.run(
                [chrome, "--version"], capture_output=True, text=True, timeout=5
            )
            m = re.search(r"(\d+)\.", out.stdout)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return 120


def _best_curl_cffi_impersonate(major: int) -> str:
    """Pick the highest curl_cffi chrome impersonation <= the real version."""
    from curl_cffi.requests import Session

    # Versions curl_cffi is known to support (sorted ascending)
    candidates = [116, 119, 120, 123, 124, 126, 127, 128, 130, 131]
    best = 120
    for v in candidates:
        if v <= major:
            try:
                Session(impersonate=f"chrome{v}").close()
                best = v
            except Exception:
                pass
    return f"chrome{best}"


CHROME_MAJOR = _detect_chrome_major()
CURL_CFFI_IMPERSONATE = _best_curl_cffi_impersonate(CHROME_MAJOR)

# Credentials (prefer .env values, fall back to os.environ)
USER_EMAIL = _env.get("USER") or os.getenv("USER")
USER_PASSWORD = _env.get("PASSWORD") or os.getenv("PASSWORD")

# Multi-account support: parse USER_N / PASSWORD_N pairs from .env
ACCOUNTS: list[dict] = []
for i in range(1, 100):
    email = _env.get(f"USER_{i}") or os.getenv(f"USER_{i}")
    password = _env.get(f"PASSWORD_{i}") or os.getenv(f"PASSWORD_{i}")
    if email and password:
        ACCOUNTS.append({"email": email, "password": password})
    else:
        break

# Fallback: if no numbered accounts, use the single USER/PASSWORD
if not ACCOUNTS and USER_EMAIL and USER_PASSWORD:
    ACCOUNTS.append({"email": USER_EMAIL, "password": USER_PASSWORD})

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)


# --- Proxy configuration ---

def get_proxy_config() -> dict | None:
    """Load 2captcha proxy config from env. Returns Playwright proxy dict or None."""
    host = _env.get("PROXY_2CAPTCHA_HOST") or os.getenv("PROXY_2CAPTCHA_HOST")
    if not host:
        return None
    port = _env.get("PROXY_2CAPTCHA_PORT") or os.getenv("PROXY_2CAPTCHA_PORT", "8080")
    protocol = _env.get("PROXY_2CAPTCHA_PROTOCOL") or os.getenv("PROXY_2CAPTCHA_PROTOCOL", "http")
    username = _env.get("PROXY_2CAPTCHA_USER") or os.getenv("PROXY_2CAPTCHA_USER")
    password = _env.get("PROXY_2CAPTCHA_PASS") or os.getenv("PROXY_2CAPTCHA_PASS")
    proxy = {"server": f"{protocol}://{host}:{port}"}
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return proxy
