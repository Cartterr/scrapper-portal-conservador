# scrapper-portal-conservador

Production-oriented local worker for the CBRS nuevo portal. The design is based on the logged-in research in `research/cbrs-network/` and the previous package at `V:\scrapper\scrapper-nuevo-portal-conservador`.

This project does not try to bypass portal protections. It uses a controlled Chrome profile for legitimate login/session state and reCAPTCHA Enterprise token generation, then performs low-volume API calls with Chrome-like HTTP behavior.

## What It Supports

- Comercio index search by razon social / text.
- Comercio index search by foja, numero, and ano.
- Ticket validation through the portal flow.
- Image reference lookup and image/PDF artifact creation.
- Manual login/session reuse through a persistent local Chrome profile.
- SQLite-backed jobs, retry state, durable live safety state, account telemetry, and artifact records.
- Sanitized diagnostics that never log raw JWTs, cookies, reCAPTCHA tokens, passwords, or ticket strings.

Propiedad, document verification, planos, and account flows are mapped in research and have adapter boundaries, but are not declared production-ready until a fresh logged-in probe validates those flows.

## Setup

```powershell
cd V:\scrapper\scrapper-portal-conservador
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
```

Copy `.env.example` to `.env` only if you want repo-local config. The implementation also reads Windows User environment variables.

Required non-secret defaults for this machine:

```powershell
[Environment]::SetEnvironmentVariable('CBRS_PORTAL_BASE_URL','https://nuevo-portal.conservador.cl','User')
[Environment]::SetEnvironmentVariable('CBRS_RECAPTCHA_SITEKEY','6Le-eiksAAAAANU-0ITcjxvGfFoHsz40juvUVI_-','User')
[Environment]::SetEnvironmentVariable('CBRS_DATA_DIR','V:\scrapper\scrapper-portal-conservador\data','User')
[Environment]::SetEnvironmentVariable('CBRS_DATABASE_URL','sqlite:///V:/scrapper/scrapper-portal-conservador/data/cbrs.sqlite3','User')
[Environment]::SetEnvironmentVariable('CBRS_BROWSER_PROFILE_DIR','V:\scrapper\scrapper-portal-conservador\.local\browser-profile','User')
[Environment]::SetEnvironmentVariable('CBRS_HEADLESS','false','User')
[Environment]::SetEnvironmentVariable('CBRS_MIN_REQUEST_DELAY_MS','30000','User')
[Environment]::SetEnvironmentVariable('CBRS_REQUEST_JITTER_PERCENT','20','User')
[Environment]::SetEnvironmentVariable('CBRS_TRANSIENT_BACKOFF_MS','120000,300000,600000','User')
```

Credentials are optional and should be supplied only through local environment variables:

```powershell
[Environment]::SetEnvironmentVariable('CBRS_USER_1','your-email@example.com','User')
[Environment]::SetEnvironmentVariable('CBRS_PASSWORD_1','your-password','User')
```

## Commands

```powershell
cbrs doctor
cbrs doctor --live
cbrs init
cbrs search --query "MBX Global"
cbrs search --foja 63244 --numero 27964 --ano 2022
cbrs download --foja 63244 --numero 27964 --ano 2022 --output .\output
cbrs enqueue search-text --query "MBX Global"
cbrs enqueue download-fna --foja 63244 --numero 27964 --ano 2022
cbrs worker --limit 5
cbrs jobs
cbrs accounts
cbrs safety status
cbrs safety events --limit 20
cbrs safety unlock --reason "checked browser session and network"
```

`cbrs init` opens a real browser profile and waits until the portal is logged in. The profile lives under `.local/browser-profile` by default and is ignored by git.

## Architecture

- `browser_session`: persistent Playwright browser profile, manual login detection, reCAPTCHA token generation, WAF cookie export.
- `portal_client`: `curl_cffi` session with Chrome TLS impersonation, cookie sync, auth refresh, durable safety lock, pacing, and error classification.
- `adapters.commerce`: Comercio search, ticket validation, image reference lookup, and image download.
- `adapters.property` and `adapters.verification`: mapped endpoint scaffolds kept disabled until logged-in probes certify those flows.
- `jobs`: SQLite job queue with retries, backoff, dedupe, live safety state, lock, and event history.
- `artifacts`: image validation, deterministic paths, SHA-256 metadata, PDF assembly.
- `cli`: operator commands for init, doctor, search, download, queue, worker, jobs, and accounts.

## Safety Model

- Do not commit `.env`, database files, browser profiles, downloaded artifacts, logs, HAR files, or raw network captures.
- Do not print raw access tokens, refresh cookies, reCAPTCHA tokens, passwords, or ticket strings.
- Treat private portal APIs as unstable implementation details.
- Stop on WAF, captcha, 429/rate-limit, challenge HTML, auth-required, and daily-limit signals instead of retrying aggressively.
- There is no local successful-request cap. The long-run safety model is signal-based: one live process, stable browser/profile/network identity, 30 second base pacing, jitter, sequential requests, and a persistent manual-required state after risky signals.
- Use `cbrs safety status` before live work if a previous run stopped. Use `cbrs safety unlock --reason "..."` only after manually checking the browser/session/network state.

## Verification

Run the offline test suite:

```powershell
python -m unittest discover -s tests
```

Run live checks only after login:

```powershell
cbrs init
cbrs doctor --live
cbrs search --query "MBX Global"
```

Before committing:

```powershell
rg -n --hidden -S "eyJ|Authorization: Bearer [A-Za-z0-9_-]|recaptcha-token: [A-Za-z0-9_-]{20,}|CBRS_PASSWORD|cbrs_refresh_token|auth_cbrs_token" .
git status --short
```

The `rg` command should not show raw secret values in tracked files.
