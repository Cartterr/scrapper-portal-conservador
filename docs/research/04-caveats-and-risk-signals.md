# Caveats And Risk Signals

## Hard Stop Signals

The system should stop live work and require operator review when any of these appear:

- HTTP `429`.
- `X-CDN: Imperva`.
- `X-Iinfo` or similar Incapsula headers.
- API endpoint returns `text/html`.
- HTML contains `incapsula`, `imperva`, `captcha`, or `access denied`.
- CBRS app code `intente-mas-tarde`.
- CBRS app code `err-limite`.
- Auth cannot be refreshed and no valid browser JWT is available.
- Unexpected redirects during API calls.
- Response body shape no longer matches expected list/dict shape.
- Image refs missing or malformed.
- Image byte endpoint returns HTML/error instead of image bytes.

## Caveats In Current Implementation

- The private CBRS endpoints are reverse-engineered from the portal and may drift.
- `curl_cffi` request shape may still differ from real browser fetch behavior.
- reCAPTCHA scoring is opaque; a valid token does not guarantee the site will accept the interaction.
- Imperva scoring is opaque; there is no known public safe threshold.
- A single successful `doctor --live` does not prove that search/download flows are safe for long runs.
- Downloading one document can produce many live requests because each page image is a request.
- Search result tickets and image refs can expire or become session-bound.
- The current app supports only Comercio as production-ready.
- Account budgets are not enforced locally by default. If CBRS itself says `err-limite`, stop.

## Operator Rules

- Before live work after an interruption, run:
  - `cbrs safety status`
- If state is `manual_required` or `auth_required`, inspect the browser/session/network manually.
- Only unlock after inspection:
  - `cbrs safety unlock --reason "checked browser session and network"`
- Do not run multiple live commands at the same time.
- Do not run live tests from different shells against the same browser profile.
- Do not change VPN/proxy/network mid-session.
- Do not switch headless/headed mode during an active browser identity.
- Do not rotate user-agent, TLS impersonation profile, or browser profile to push through a stop signal.
- Do not repeatedly retry after `429`, WAF, CAPTCHA, or `err-limite`.

## Testing Caveats

- Offline tests can validate classification, lock behavior, pacing calls, and state transitions.
- Offline tests cannot prove CBRS/Imperva will accept a live pattern.
- Live tests should be narrow:
  - one known no-result search;
  - one known positive search;
  - one known small download;
  - then observe safety events.
- Long soaks should be treated as production operation, not threshold discovery.
