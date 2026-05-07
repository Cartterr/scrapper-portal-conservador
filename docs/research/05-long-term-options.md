# Long-Term Options

This project currently uses cautious portal automation. The options below are possible paths toward a more permanent, less fragile solution.

## 1. Official Or Partner Access

Best long-term option if available.

- Ask CBRS whether an official API, institution account, bulk lookup workflow, convenio, or documented data access path exists.
- Prefer a signed/approved integration over private portal endpoint use.
- Clarify allowed request volume, account model, rate limits, and data retention rules.
- Ask for a support contact and documented recovery path for WAF/captcha false positives.

## 2. Human-In-The-Loop Browser Tool

Lower automation risk than private API replay.

- Keep a real browser tab/session as the primary execution surface.
- Let the operator perform login/challenges manually.
- Use automation only to assist with repetitive form fill, result capture, and artifact organization.
- Avoid direct API calls where browser UI interaction is acceptable.

## 3. Portal-Compatible Service Worker

Middle-ground option.

- Keep the persistent browser open.
- Execute fetches from inside the browser context instead of replaying requests from `curl_cffi`.
- Benefits:
  - browser cookies/storage/JS state stay native;
  - fewer differences from portal-origin fetch behavior.
- Costs:
  - harder error handling;
  - browser must stay running;
  - still private endpoint usage.

## 4. Request Shape Verification Harness

Useful for safer maintenance.

- Build a sanitized comparison harness that records:
  - browser-origin request metadata;
  - client-origin request metadata;
  - response class only, not raw tokens or documents.
- Alert when headers, endpoints, actions, or response shapes drift.
- Never commit raw captures.

## 5. Dedicated Account/Profile Registry

Useful if operation expands beyond one account.

- One account maps to one browser profile and one stable network identity.
- Store only labels and hashes, not credentials.
- Add operator status for each profile:
  - `ok`
  - `manual_required`
  - `auth_required`
  - `retired`
- Never auto-rotate accounts to bypass CBRS app limits.

## 6. Stronger Artifact Pipeline

Useful for reliability without increasing portal pressure.

- Cache successful public search result metadata.
- Avoid re-downloading documents already present by SHA-256/path.
- Add resumable downloads only when safe:
  - resume from local completed pages;
  - still pace every live request;
  - stop on any portal risk signal.

## 7. Formal Runbook And Monitoring

Useful for long-running local operation.

- Add an operator dashboard or text report around:
  - safety state;
  - last live request;
  - last risk signal;
  - last unlock reason;
  - current lock owner;
  - artifacts created.
- Keep alerts local and sanitized.
- Prefer "manual-required" states over automated recovery.

## Recommended Direction

The best durable path is official/partner access. Until then, the safest local strategy is:

- persistent browser profile;
- one live process;
- stable network identity;
- browser-generated reCAPTCHA;
- signal-based manual stops;
- no retrying through WAF/captcha/rate-limit signals;
- rich sanitized evidence for debugging drift.
