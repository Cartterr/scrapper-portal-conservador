# Platform Limitations

## CBRS / Account Constraints

- Login credentials are personal and confidential under CBRS terms.
- Comercio requests can be account-sensitive and session-sensitive.
- App-level `err-limite` should be treated as an account-side stop signal, not something to retry through.
- Because CBRS does not publish a stable API contract for these endpoints, endpoint paths, body fields, headers, and response shapes can drift without notice.

## reCAPTCHA Enterprise Constraints

- reCAPTCHA Enterprise is part of the CBRS login/portal flow.
- Tokens are short-lived and one-time use. Google documents that reCAPTCHA response tokens must be verified within two minutes and can only be verified once.
- For Enterprise/action integrations, Google says the backend should verify that token `action` matches the expected action.
- Browser-side JavaScript readiness matters. If the reCAPTCHA script cannot load or `grecaptcha.enterprise.ready` does not complete, web API calls should not proceed.
- Practical local rule:
  - Generate token immediately before the request.
  - Never reuse a token.
  - Discard token if it is older than 90 seconds locally.
  - Stop on first CBRS `intente-mas-tarde`.

## Imperva / Incapsula Constraints

- Imperva can apply static, challenge-based, and behavioral bot detection.
- Challenge-based checks may validate cookies, JavaScript execution, and CAPTCHA behavior.
- Behavioral checks can compare a visitor’s actions against expected browser/user baselines.
- Imperva materials describe rate limiting by requesting client or machine, not only by IP.
- Imperva WAF custom rules can request CAPTCHA/JavaScript/cookie validation, block requests, block sessions, or block IPs.
- Public reports and product docs indicate risk is tied to combined identity signals:
  - browser profile and cookies;
  - IP/network reputation;
  - browser/user-agent freshness;
  - device/browser fingerprint;
  - request cadence;
  - API access patterns;
  - whether behavior looks like normal navigation.

## Browser Profile Constraints

- Playwright persistent contexts store browser data in a user data directory.
- Browsers do not support multiple instances using the same user data directory at once.
- Playwright docs warn against automating the user's regular Chrome profile; use a separate automation profile.
- Public operator reports say fresh contexts can lose localStorage/IndexedDB/session state, while persistent contexts preserve more of the auth surface.
- Practical local rule:
  - One CBRS account/profile/network identity at a time.
  - One live process at a time.
  - Do not rotate IP/proxy/user-agent during a logged-in session.

## Network Identity Constraints

- A working phone on 5G while the PC/network is blocked suggests local IP/device/browser identity can be penalized independently of the CBRS account.
- Public operator reports about protected sites warn that stateful logged-in sessions should not move between rotating IPs.
- Practical local rule:
  - Keep network identity stable.
  - Do not switch VPN/proxy mid-session.
  - Treat any WAF/captcha/rate-limit signal as requiring manual inspection.
