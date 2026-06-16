"""Test harness for progressive HTTP-based scraping experiments.

Probes whether curl_cffi can replace Playwright for CBRS API calls.

Usage:
    python -m cbrs.scraper_http phase1   # curl_cffi WAF bypass
    python -m cbrs.scraper_http phase2   # reCAPTCHA enforcement
    python -m cbrs.scraper_http phase3   # CapSolver token generation
    python -m cbrs.scraper_http all      # Run all phases
"""

import argparse
import json
import logging

from curl_cffi.requests import Session

from . import config

logger = logging.getLogger(__name__)

COMMERCE_INDEX_URL = (
    f"{config.BASE_URL}"
    "/consultas-en-linea/indices/indice-del-registro-de-comercio"
)

CHROME_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": config.BASE_URL,
    "Referer": COMMERCE_INDEX_URL,
    "sec-ch-ua": (
        '"Not_A Brand";v="8", "Chromium";v="120", '
        '"Google Chrome";v="120"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def phase1_waf_probe() -> bool:
    """Phase 1: Test if curl_cffi can pass Imperva WAF."""
    print("\n" + "=" * 60)
    print("PHASE 1: curl_cffi WAF Probe")
    print("=" * 60)

    session = Session(impersonate=config.CURL_CFFI_IMPERSONATE)

    # Step 1: Load the page to get WAF cookies
    print("\n[1] GET commerce index page...")
    resp = session.get(COMMERCE_INDEX_URL)
    print(f"    Status: {resp.status_code}")
    print(f"    Cookies: {dict(session.cookies)}")

    cookie_names = list(session.cookies.keys())
    has_visid = any("visid_incap" in c for c in cookie_names)
    has_incap_ses = any("incap_ses" in c for c in cookie_names)
    print(f"    visid_incap_*: {'FOUND' if has_visid else 'MISSING'}")
    print(f"    incap_ses_*: {'FOUND' if has_incap_ses else 'MISSING'}")

    if resp.status_code != 200:
        print(f"\n    FAIL: Got {resp.status_code} instead of 200")
        if "Incapsula" in resp.text or "_Incapsula_Resource" in resp.text:
            print("    FAIL: Received Imperva challenge page")
        return False

    # Step 2: POST to /home/start
    print("\n[2] POST /api/v1/home/start...")
    resp = session.post(
        f"{config.BASE_URL}/api/v1/home/start",
        headers=CHROME_HEADERS,
        json={"preHint": ""},
    )
    print(f"    Status: {resp.status_code}")
    try:
        print(f"    Body: {resp.json()}")
    except Exception:
        print(f"    Body: {resp.text[:200]}")

    if resp.status_code == 200:
        print("\n    PASS: curl_cffi passes Imperva WAF!")
        return True

    if resp.status_code == 403:
        print("\n    FAIL: 403 Forbidden — WAF blocked the request")
        return False

    print(f"\n    UNKNOWN: Unexpected status {resp.status_code}")
    return False


def phase2_recaptcha_enforcement(cookies: dict | None = None) -> dict:
    """Phase 2: Test reCAPTCHA enforcement on various endpoints."""
    print("\n" + "=" * 60)
    print("PHASE 2: reCAPTCHA Enforcement Probe")
    print("=" * 60)

    session = Session(impersonate=config.CURL_CFFI_IMPERSONATE)
    session.get(COMMERCE_INDEX_URL)

    if cookies:
        for name, value in cookies.items():
            session.cookies.set(name, value)

    # Warm up session
    session.post(
        f"{config.BASE_URL}/api/v1/home/start",
        headers=CHROME_HEADERS,
        json={"preHint": ""},
    )

    endpoints = [
        ("POST", "/api/v1/home/start", {"preHint": ""}, "No captcha expected"),
        ("POST", "/api/v1/auth/refresh", {}, "Refresh token (no captcha?)"),
        (
            "POST",
            "/api/v1/auth/login",
            {
                "email": config.ACCOUNTS[0]["email"],
                "password": config.ACCOUNTS[0]["password"],
            },
            "Login WITHOUT captcha token",
        ),
    ]

    results = {}
    for method, path, body, desc in endpoints:
        print(f"\n[*] {method} {path} — {desc}")
        hdrs = {**CHROME_HEADERS}

        resp = session.post(
            f"{config.BASE_URL}{path}",
            headers=hdrs,
            json=body,
        )

        print(f"    Status: {resp.status_code}")
        try:
            data = resp.json()
            print(f"    Body: {json.dumps(data, ensure_ascii=False)[:200]}")
        except Exception:
            print(f"    Body: {resp.text[:200]}")

        results[path] = resp.status_code

    # Test login WITH empty captcha token
    print("\n[*] POST /api/v1/auth/login — Login WITH empty captcha token")
    hdrs = {**CHROME_HEADERS, "recaptcha-token": ""}
    resp = session.post(
        f"{config.BASE_URL}/api/v1/auth/login",
        headers=hdrs,
        json={
            "email": config.ACCOUNTS[0]["email"],
            "password": config.ACCOUNTS[0]["password"],
        },
    )
    print(f"    Status: {resp.status_code}")
    try:
        print(f"    Body: {json.dumps(resp.json(), ensure_ascii=False)[:200]}")
    except Exception:
        print(f"    Body: {resp.text[:200]}")

    return results


def phase3_capsolver() -> bool:
    """Phase 3: Test CapSolver reCAPTCHA Enterprise token generation."""
    print("\n" + "=" * 60)
    print("PHASE 3: CapSolver Token Probe")
    print("=" * 60)

    capsolver_key = config.CAPSOLVER_API_KEY
    if not capsolver_key:
        print("\n    SKIP: CAPSOLVER_API_KEY not set in .env")
        print("    Set it and re-run to test CapSolver integration.")
        return False

    try:
        import capsolver  # noqa: F811
    except ImportError:
        print("\n    SKIP: capsolver package not installed")
        print("    Run: pip install capsolver")
        return False

    capsolver.api_key = capsolver_key

    # Step 1: Generate token for login action
    print(
        "\n[1] Generating reCAPTCHA Enterprise token via CapSolver "
        "(action=login)..."
    )
    try:
        solution = capsolver.solve(
            {
                "type": "ReCaptchaV3EnterpriseTaskProxyLess",
                "websiteURL": COMMERCE_INDEX_URL,
                "websiteKey": config.RECAPTCHA_SITEKEY,
                "pageAction": "login",
            }
        )
        token = solution.get("gRecaptchaResponse", "")
        print(f"    Token received ({len(token)} chars)")
    except Exception as e:
        print(f"    FAIL: CapSolver error: {e}")
        return False

    # Step 2: Try to login with CapSolver token
    print("\n[2] Testing CapSolver token against /api/v1/auth/login...")
    session = Session(impersonate=config.CURL_CFFI_IMPERSONATE)
    session.get(COMMERCE_INDEX_URL)
    session.post(
        f"{config.BASE_URL}/api/v1/home/start",
        headers=CHROME_HEADERS,
        json={"preHint": ""},
    )

    hdrs = {**CHROME_HEADERS, "recaptcha-token": token}
    resp = session.post(
        f"{config.BASE_URL}/api/v1/auth/login",
        headers=hdrs,
        json={
            "email": config.ACCOUNTS[0]["email"],
            "password": config.ACCOUNTS[0]["password"],
        },
    )
    print(f"    Status: {resp.status_code}")
    try:
        data = resp.json()
        print(f"    Body: {json.dumps(data, ensure_ascii=False)[:200]}")
    except Exception:
        data = {}
        print(f"    Body: {resp.text[:200]}")

    if resp.status_code != 200:
        print("\n    FAIL: CapSolver token rejected")
        return False

    print("\n    PASS: CapSolver token accepted!")

    # Step 3: Try search with CapSolver token
    jwt = data.get("token", "")
    print(
        "\n[3] Generating search token via CapSolver "
        "(action=indice_com_texto)..."
    )
    try:
        solution = capsolver.solve(
            {
                "type": "ReCaptchaV3EnterpriseTaskProxyLess",
                "websiteURL": COMMERCE_INDEX_URL,
                "websiteKey": config.RECAPTCHA_SITEKEY,
                "pageAction": "indice_com_texto",
            }
        )
        search_token = solution.get("gRecaptchaResponse", "")
        print(f"    Token received ({len(search_token)} chars)")
    except Exception as e:
        print(f"    FAIL: CapSolver error: {e}")
        return False

    print("\n[4] Testing search with CapSolver token...")
    hdrs = {
        **CHROME_HEADERS,
        "Authorization": f"Bearer {jwt}",
        "recaptcha-token": search_token,
    }
    resp = session.post(
        f"{config.BASE_URL}/api/v1/comercio/indice/texto",
        headers=hdrs,
        json={
            "foja": None,
            "numero": None,
            "ano": None,
            "texto": "MBX Global",
            "recaptchaToken": search_token,
            "ticket": None,
            "titulosAnteriores": False,
            "comuna": None,
            "anoP": None,
            "origen": "texto",
        },
    )
    print(f"    Status: {resp.status_code}")
    try:
        print(f"    Body: {json.dumps(resp.json(), ensure_ascii=False)[:300]}")
    except Exception:
        print(f"    Body: {resp.text[:300]}")

    if resp.status_code == 200:
        print("\n    PASS: Full HTTP-only scraping works with CapSolver!")
        return True

    print("\n    FAIL: Search token rejected")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="CBRS HTTP scraping experiment harness",
    )
    parser.add_argument(
        "phase",
        choices=["phase1", "phase2", "phase3", "all"],
        help="Which phase to run",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.phase in ("phase1", "all"):
        phase1_waf_probe()

    if args.phase in ("phase2", "all"):
        phase2_recaptcha_enforcement()

    if args.phase in ("phase3", "all"):
        phase3_capsolver()

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


if __name__ == "__main__":
    main()
