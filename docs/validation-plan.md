# CBRS Fixed-Trust Validation Plan

This plan proves the tool works inside the intended constraint: one legitimate
operator, one clean persistent Chrome/Edge profile, one declared non-personal
Chilean egress path, slow sequential requests, no retries, no solver/account
rotation, and hard stops on portal risk signals.

## 1. Local Safety Gate

Run before any live portal action:

```powershell
python -m compileall cbrs tests
python -m pytest
python -m cbrs doctor
python -m cbrs preflight
python -m cbrs preflight --approve-egress-baseline
rg -n "CapSolver|capsolver|2captcha|ACCOUNTS|USER_\d|PASSWORD_\d|cbrs_session|disable-blink|AutomationControlled|CBRS_CLOAK_PROXY_URL"
```

Acceptance:

- compile succeeds
- tests pass
- `doctor` is all OK only after `CBRS_EGRESS_MODE` is set to an approved
  non-personal mode
- plain `preflight` blocks if no approved baseline exists
- `preflight --approve-egress-baseline` is run only after the operator confirms
  they are on the client-owned Chilean egress path
- after approval, plain `preflight` passes only from the same egress hash
- search results only show docs/tests/legacy code, not active production config
- `.env` does not contain `CBRS_CLOAK_PROXY_URL`
- `.env` may contain `CBRS_PROXY_URL` only when
  `CBRS_EGRESS_MODE=dedicated_static_isp`; never commit the real proxy URL
- `.env` contains one of:
  `CBRS_EGRESS_MODE=client_vpn`,
  `CBRS_EGRESS_MODE=client_office`, or
  `CBRS_EGRESS_MODE=dedicated_static_isp`
- live validation runs use `CBRS_HEADLESS=0` by default because the portal has
  rejected headless commerce searches; `--headless` is troubleshooting-only
- use `CBRS_WINDOW_MODE=offscreen` when the headed browser should not occupy the
  main desktop
- last-resort personal/direct testing requires both
  `CBRS_EGRESS_MODE=personal_direct` and `CBRS_ALLOW_PERSONAL_EGRESS=1`
  and must not be treated as production validation

## 2. Manual Login Gate

```powershell
python -m cbrs init --timeout 600
```

Acceptance:

- preflight passes first
- headed Chrome/Edge opens with `.cbrs/chrome-profile`
- operator logs in manually
- command exits after detecting the login cookie
- no raw cookie/session JSON is created

## 3. Day 1 Search-Only Live Proof

Use one known safe query or FNA provided by the operator:

```powershell
python -m cbrs validate --query "KNOWN_SAFE_NAME"
```

Acceptance:

- preflight passes and the fixed-egress hash matches the saved baseline
- exactly one search flow is attempted
- normal fixed request delay is applied
- result count is printed
- sanitized report is written to `.cbrs/logs/validation-*.json`
- report does not store query text, ticket, cookies, JWTs, captcha tokens, raw
  IPs, credentials, or proxy URLs
- report stores browser backend, browser family, profile hash, egress country,
  egress hash, fixed delay, sanitized proxy metadata, and safety-stop reason if
  any

## 4. Day 2 Search-Only Live Proof

Repeat one safe search from the same profile and same approved egress path:

```powershell
python -m cbrs validate --query "KNOWN_SAFE_NAME"
```

Acceptance:

- preflight egress hash still matches baseline
- profile remains logged in, or manual login can be repeated without automation
- no `403`, `429`, `err-limite`, `intente-mas-tarde`, challenge HTML, login
  failure, egress drift, or account lockout

## 5. Day 3 Search Plus Optional First Download

Only after Day 1 and Day 2 pass:

```powershell
python -m cbrs validate --query "KNOWN_SAFE_NAME"
python -m cbrs validate --query "KNOWN_SAFE_NAME" --download-first
```

Acceptance:

- one search flow is attempted per command
- optional download uses only the first result
- image pages are downloaded sequentially
- PDF exists and has non-zero size
- sanitized report includes PDF path and size only

## 6. Long-Running Soak Proof

Use the soak runner when the goal is to prove the normal flow over time without
waiting on a manual multi-day checklist:

```powershell
python -m cbrs soak dashboard
python -m cbrs soak run --dry-run --max-cycles 3 --dashboard
python -m cbrs soak run --dashboard
```

Acceptance:

- standalone dashboard starts without creating a run or touching the portal
- dry-run writes local soak history and placeholder output without portal traffic
- live soak starts with one immediate full-flow cycle
- later live cycles wait a randomized test-only `2-4` minute interval,
  averaging about 20 full-flow consults per hour
- every live cycle uses preflight, the persistent profile, safe search, and one
  first-result download
- PDFs are written under `outputs/soak/<run_id>/<cycle_id>/`
- the dashboard at `http://127.0.0.1:8765` shows status, uptime, heartbeat,
  success rate, safety stops, validation reports, and output artifacts
- `python -m cbrs soak stop` or the dashboard Stop button requests a graceful
  stop after the current safe point
- any hard safety stop leaves the dashboard alive but blocks all future portal
  actions until operator review

## 7. Safety Stop Proofs

These are logic tests, not live stress tests:

- `tests/test_safety.py` proves `err-limite`, `intente-mas-tarde`, `403`, `429`,
  WAF/challenge HTML, and image HTML responses stop the flow.
- `tests/test_preflight.py` proves legacy proxy config, non-CL egress, invalid
  browser-proxy mode, and egress-hash drift stop before portal traffic.
- The live tool must not continue, retry, rotate accounts, or switch identity
  after any safety stop.

## Operating Constraints

- one operator session at a time
- no bulk jobs
- no parallel downloads
- no retry loops
- no IPRoyal Residential or rotating proxy in production
- do not use a personal/home IP for approval or live validation
- use client-owned Chilean egress, a client VPN, or a dedicated static Chile ISP
  path approved in writing
- if using `CBRS_PROXY_URL`, keep it fixed per account/profile and use only
  `CBRS_EGRESS_MODE=dedicated_static_isp`
- if personal/direct mode is used anyway, label it explicitly and stop after the
  minimum login/search proof
- no continuing after egress drift, `403`, `429`, `err-limite`,
  `intente-mas-tarde`, or challenge HTML
- keep default request delay at a fixed `5.0s` unless a slower setting is needed

## Client-Facing Summary

The proof is not based on load testing. It is based on proving the tool behaves
like a careful operator from a stable trusted environment: persistent
Chrome/Edge login, low volume, sequential actions, sanitized logs, fixed egress,
and immediate stop on any portal risk signal.
