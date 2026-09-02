# CBRS Commerce Registry Operator Tool

This branch keeps the original commerce-registry flow but runs endurance on
native Windows through installed Google Chrome, one persistent profile and fixed Chilean
egress per authorized account, automatic browser-origin authentication, fixed
pacing, a durable SQLite job queue, and hard stops on global risk signals.

Production defaults to browser-owned CAPTCHA tokens and can opt into one
2Captcha fallback. It does not use proxy cycling, raw-cookie export, or stealth
browser defaults. The scheduler may fail over between separately authorized
accounts, but never changes an account's profile or egress identity.
CloakBrowser/IPRoyal support is legacy opt-in only and is not the production
trust path.

## Production Model

```text
cbrs doctor
  Verifies production config uses the chrome backend
  Verifies Chrome/Edge can be found
  Verifies legacy Cloak proxy config is absent
  Allows browser proxy only for dedicated static ISP mode
  Verifies the egress mode is explicitly declared
  Verifies local secret/profile paths are ignored by git

cbrs preflight
  Checks installed Chrome/Edge and profile metadata
  Confirms legacy Cloak proxy env is not configured
  Confirms any browser proxy is only used with dedicated static ISP mode
  Refuses public egress lookup until egress mode is declared
  Looks up public egress country
  Requires CL by default
  Requires explicit approval before creating the first egress hash baseline
  Checks the fixed-egress hash baseline after approval
  Writes a sanitized report under .cbrs/logs/

cbrs init
  Runs preflight first
  Runs proxy health when a fixed browser proxy is configured
  Opens headed Chrome/Edge with .cbrs/chrome-profile
  Keeps Chromium sandbox enabled and does not bypass CSP
  Operator logs in manually
  Browser storage persists locally
  No raw cookie/session JSON is exported

cbrs jobs worker
  Claims idempotent requests from SQLite with a single-worker lease
  Selects the next eligible account with a persistent strict round-robin cursor
  Refreshes or automatically authenticates inside that account's Chrome profile
  Reserves quota immediately before each real search
  Downloads every returned inscription and publishes hashed PDFs atomically
  Recovers abandoned jobs after an expired lease without repeating completed items

cbrs jobs dashboard
  Binds the dashboard and JSON API to loopback only
  Accepts jobs, exposes status/artifacts, cancellation, quotas and backup health
  Starts headed CAPTCHA recovery while all normal traffic remains paused

cbrs validate
  Runs preflight before any portal request
  Runs proxy health before browser work when CBRS_PROXY_URL is configured
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
  jobs.py              Queue, leases, account scheduling, artifacts, and recovery
  backup.py            Online SQLite snapshot, restic, and storage health
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

CAPTCHA pauses only the affected account after the configured one-shot fallback
is exhausted and permits another already-authorized account to continue.
Rate limits and WAF challenges are global hard stops. Solver network/capacity
failures open a 15-minute circuit; authentication or zero balance disables only
the paid fallback while browser-token traffic may continue.
The operator action after a global stop is review or official escalation from
the same approved environment.

## Environment

Production settings are optional and prefixed with `CBRS_`:

```env
CBRS_BROWSER_BACKEND=chrome
CBRS_BROWSER_EXECUTABLE_PATH=
CBRS_PROFILE_DIR=.cbrs/chrome-profile
CBRS_HEADLESS=1
CBRS_WINDOW_MODE=offscreen
CBRS_EGRESS_MODE=client_vpn
CBRS_EXPECTED_EGRESS_COUNTRY=CL
CBRS_PROXY_URL=
CBRS_OUTPUT_DIR=outputs
CBRS_REQUEST_DELAY_SECONDS=5.0
CBRS_USE_CURL_CFFI_FOR_IMAGES=0
```

`CBRS_BROWSER_EXECUTABLE_PATH` is only needed when auto-detection cannot find
Chrome or Edge. Auto-detection checks Chrome first, then Edge.

The native unattended worker runs installed Chrome headless. Manual recovery
first pauses/stops the worker and then opens only the affected persistent
profile headed. `CBRS_WINDOW_MODE=offscreen` remains a local diagnostic option,
not the unattended endurance path.

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

`CBRS_PROXY_URL` is the generic fixed browser proxy setting. It is allowed only
when `CBRS_EGRESS_MODE=dedicated_static_isp`, and should point to one stable
Chile ISP endpoint. Reports redact the full proxy URL and store only scheme,
port, and a host hash. Do not use it for rotating proxy pools or fallback after
blocks.

`CBRS_CAPTCHA_SOLVER_MODE=2captcha_manual` keeps browser-owned Enterprise v3
tokens as the primary path. Rejection pauses the account without calling the
provider; an operator must arm one `RecaptchaV3TaskProxyless` solve for that
account. The API key is read only from
`CBRS_2CAPTCHA_API_KEY`. See `docs/2captcha-long-run.md`.

`python -m cbrs pool proxy-health` checks each configured pool account proxy
before login attempts: Chile egress, Google reCAPTCHA Enterprise script loading,
and CBRS `/api/v1/home/start`. A proxy that fails this gate is not production
usable even if its public IP geolocates to Chile.

`CBRS_USE_CURL_CFFI_FOR_IMAGES=1` is a compatibility transport only for binary
image downloads. The default remains browser-origin fetch to preserve one
session identity.

## Local Checks

```bash
python -m compileall cbrs tests
python -m pytest -q
python -m cbrs doctor
python -m cbrs pool proxy-health --approve-egress-baseline
python -m cbrs jobs status
```
