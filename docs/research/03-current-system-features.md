# Current System Features

## Package And CLI

- Installable Python package with `cbrs` CLI.
- Editable local venv setup.
- Local config through `.env` or Windows User environment variables.
- Commands include:
  - `doctor`
  - `doctor --live`
  - `init`
  - `search`
  - `download`
  - `enqueue`
  - `worker`
  - `jobs`
  - `accounts`
  - `safety status`
  - `safety events`
  - `safety unlock`

## Browser Session

- Uses a persistent local Playwright browser profile.
- Supports manual login through `cbrs init`.
- Reads current JWT from browser localStorage.
- Syncs cookies into the HTTP client.
- Generates reCAPTCHA Enterprise tokens from the browser page.
- Uses a dedicated profile path, not the main user Chrome profile.

## Portal Client

- Uses `curl_cffi` with Chrome impersonation.
- Centralizes live request pacing.
- Adds auth and reCAPTCHA headers where needed.
- Refreshes auth where possible.
- Classifies portal responses into stable error categories.
- Detects WAF/challenge HTML on API routes.

## Safety Model

- No fixed local successful-request quota.
- Signal-based stop model:
  - WAF/Imperva/Incapsula markers.
  - Challenge HTML on API endpoints.
  - `429`.
  - `intente-mas-tarde`.
  - `err-limite`.
  - unresolved auth-required state.
- Persistent SQLite safety state:
  - `ok`
  - `manual_required`
  - `auth_required`
- Persistent safety events for operator visibility.
- Global live-session lock prevents concurrent live portal commands.
- Default pacing:
  - 30 second base delay.
  - 20 percent jitter.
  - transient backoff sequence: 2m, 5m, 10m.
- reCAPTCHA freshness:
  - fresh token per request;
  - one-shot local use;
  - local max token age: 90 seconds.

## Jobs And Artifacts

- SQLite job queue with dedupe and retry state.
- Worker stops after safety/manual-required transitions.
- Download preflight prints page image count before fetching images.
- Page image requests are sequential and paced.
- PDF artifacts store path, content type, SHA-256, byte count, and page count.

## Privacy And Sanitization

- Redacted logging.
- Secret-name redaction in diagnostics.
- No raw JWT/cookie/reCAPTCHA/ticket values in normal CLI output.
- Runtime directories are ignored by git:
  - `.local/`
  - `data/`
  - output/runtime artifacts.
