# CBRS Commerce Registry Operator Tool

This branch keeps the original commerce-registry flow but runs it through a
fixed-trust production model: normal Chrome/Edge, one clean persistent profile,
manual login, a declared non-personal Chilean egress path, fixed pacing, and
hard stops on risk signals.

Production does not use CAPTCHA solving, account rotation, proxy cycling,
automated credential login, raw-cookie export, or stealth browser defaults.
CloakBrowser/IPRoyal support is legacy opt-in only and is not the production
trust path.

## Production Model

```text
cbrs doctor
  Verifies production config uses the chrome backend
  Verifies Chrome/Edge can be found
  Verifies proxy config is absent
  Verifies the egress mode is explicitly declared
  Verifies local secret/profile paths are ignored by git

cbrs preflight
  Checks installed Chrome/Edge and profile metadata
  Confirms no local proxy env is configured
  Refuses public egress lookup until egress mode is declared
  Looks up public egress country
  Requires CL by default
  Requires explicit approval before creating the first egress hash baseline
  Checks the fixed-egress hash baseline after approval
  Writes a sanitized report under .cbrs/logs/

cbrs init
  Runs preflight first
  Opens headed Chrome/Edge with .cbrs/chrome-profile
  Operator logs in manually
  Browser storage persists locally
  No raw cookie/session JSON is exported

cbrs validate
  Runs preflight before any portal request
  Reuses the persistent browser profile in headed mode by default
  Can move the headed browser offscreen with CBRS_WINDOW_MODE=offscreen
  Uses fixed 5.0s pacing and browser-origin fetch
  Writes a sanitized validation report
  Stops on egress drift, auth failure, rate-limit, WAF, captcha, or challenge signals
```

## Files

```text
cbrs/
  browser_runtime.py   Chrome/Edge detection and profile metadata hashing
  browser_session.py   Persistent browser context and same-origin fetch
  preflight.py         Fixed-egress checks and sanitized preflight reports
  client.py            Auth refresh, pacing, response safety checks
  safety.py            Stop classification and redaction
  validation.py        Sanitized low-volume validation report writer
  scraper.py           Commerce search/download domain flow
  pdf.py               Pure PDF assembly utilities
  config.py            CBRS_* environment parsing and safe defaults
  cli.py               init, doctor, preflight, search, download, validate
```

Removed legacy files:

- `scraper_http.py`: HTTP/WAF/captcha-solver experiment harness.
- `recaptcha.py`: stealth/proxy/fallback-login token generator.
- `session.py`: raw cookie JSON storage.

## Hard Stops

The client raises a safety stop instead of retrying or rotating identity for:

- fixed-egress preflight failure or egress hash drift
- `err-limite`
- `intente-mas-tarde`
- portal temporary-unavailable JSON asking to try later
- HTTP `401`, `403`, or `429`
- Imperva/Incapsula challenge HTML or headers
- protected endpoints returning HTML where JSON/image data is expected
- unexpected non-200 statuses

The correct operator action after a stop is manual review, official access
escalation, or trying again later from the same approved environment.

## Environment

Production settings are optional and prefixed with `CBRS_`:

```env
CBRS_BROWSER_BACKEND=chrome
CBRS_BROWSER_EXECUTABLE_PATH=
CBRS_PROFILE_DIR=.cbrs/chrome-profile
CBRS_HEADLESS=0
CBRS_WINDOW_MODE=offscreen
CBRS_EGRESS_MODE=client_vpn
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_OUTPUT_DIR=outputs
CBRS_REQUEST_DELAY_SECONDS=5.0
CBRS_USE_CURL_CFFI_FOR_IMAGES=0
```

`CBRS_BROWSER_EXECUTABLE_PATH` is only needed when auto-detection cannot find
Chrome or Edge. Auto-detection checks Chrome first, then Edge.

`CBRS_HEADLESS=0` is the supported live portal mode. `cbrs init` is always
headed because manual login requires a visible browser. `--headless` remains
available for local troubleshooting, but the 2026-06-18 live proof showed the
portal can reject headless commerce searches with a temporary 400 response while
the same profile succeeds headed.

`CBRS_WINDOW_MODE=offscreen` keeps the browser headed but moves the window away
from the visible desktop using Chrome window position arguments. Use
`CBRS_WINDOW_MODE=normal` if Windows pulls the window back onscreen or if manual
inspection is needed.

`CBRS_EGRESS_MODE` is mandatory before live operations. Allowed production
values are:

- `client_vpn`
- `client_office`
- `dedicated_static_isp`

Do not approve a production baseline from a personal/home IP. For an explicit
last-resort personal/direct test, set both:

```env
CBRS_EGRESS_MODE=personal_direct
CBRS_ALLOW_PERSONAL_EGRESS=1
```

This mode is intentionally not production-safe; reports will label it as
`personal_direct`.

`CBRS_CLOAK_PROXY_URL` is not allowed in production fixed-egress mode. Keep it
out of `.env` before running `doctor`, `preflight`, `init`, `search`,
`download`, or `validate`.

`CBRS_USE_CURL_CFFI_FOR_IMAGES=1` is a compatibility transport only for binary
image downloads. The default remains browser-origin fetch to preserve one
session identity.

## Local Checks

```powershell
python -m compileall cbrs tests
python -m pytest
python -m cbrs doctor
python -m cbrs preflight
python -m cbrs preflight --approve-egress-baseline
```
