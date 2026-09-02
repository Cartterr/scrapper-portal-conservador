# CapSolver proxy-bound Enterprise v3 experiment

## Why this is different from the 2Captcha experiment

CBRS uses invisible reCAPTCHA Enterprise v3 with action `login` for login and
`indice_com_texto` for Commerce searches. The completed 2Captcha matrix could
not bind an Enterprise-v3 token to the browser's proxy and user-agent context.

CapSolver documents `ReCaptchaV3EnterpriseTask` (without `ProxyLess`) as a task
that requires the caller's own proxy. Its current Core SDK also models a custom
user-agent for token tasks. This experiment therefore sends:

- the exact CBRS page URL, site key and action;
- the existing account-specific Chilean proxy URL;
- the user-agent read from that account's headed Chrome page;
- minimum score `0.9`.

If CapSolver returns the user-agent/client-hint identity actually used by its
worker, that returned identity is authoritative. The headed Chrome target is
aligned through CDP before the token is submitted and the override is kept
active until the browser session closes.

The account/profile/proxy mapping is not changed. CapSolver receives the proxy
only for the paid token task; the same proxy remains installed in the local
browser session that submits the token.

Official references:

- [reCAPTCHA v3 and Enterprise v3 task types](https://docs.capsolver.com/en/guide/captcha/ReCaptchaV3/)
- [supported proxy formats](https://docs.capsolver.com/en/guide/api-how-to-use-proxy/)
- [Core SDK parameters](https://docs.capsolver.com/en/guide/ai/core-sdk/)
- [current pricing](https://docs.capsolver.com/en/pricing/)

## Runtime configuration

Secrets stay only in `C:\ProgramData\CBRS\cbrs.env`:

```dotenv
CBRS_CAPTCHA_SOLVER_MODE=capsolver_manual
CBRS_CAPSOLVER_API_KEY=REDACTED
CBRS_CAPSOLVER_TIMEOUT_SECONDS=120
CBRS_CAPSOLVER_POLL_SECONDS=3
```

`capsolver_manual` preserves browser-native Google tokens as the normal path.
One paid attempt requires an explicit authorization unless the dashboard's
automatic-solver switch is deliberately enabled. A bounded diagnostic may use
direct `capsolver` mode in one foreground process without enabling scheduled or
indefinite work.

For an A/B control, `captcha-test --provider browser` runs the same headed,
search-only request with a browser-native Google token. It uses the same account,
profile, proxy and fixture and likewise never downloads a PDF.

## Acceptance criteria

The provider API health check must report a positive balance without creating a
task. A paid live attempt is successful only if all of these hold:

1. preflight proves Chilean egress and matches the account baseline;
2. CapSolver returns a token through `ReCaptchaV3EnterpriseTask` using that same
   proxy and browser user-agent;
3. CBRS accepts the token and returns a normal login or search response;
4. the sanitized attempt row records provider `capsolver`, cost/latency and the
   CBRS outcome without storing the token, API key, proxy URL or task ID.

A provider token with a CBRS error is not success. The experiment remains
bounded and does not restart the indefinite scheduler.
