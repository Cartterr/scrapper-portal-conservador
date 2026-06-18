# Fixed-Trust Production Runtime

## Decision

Production uses normal Chrome/Edge with one clean persistent profile and a
declared non-personal Chilean egress path. This aligns the automated flow with
the operator's authorized access pattern while avoiding the user's personal/home
IP, residential proxy trust, and stealth-browser reputation.

IPRoyal Residential is not the production path because residential proxy pools
can rotate, inherit unrelated reputation, and trigger portal risk controls even
when the tool itself is low-volume. It remains useful only for isolated
connectivity experiments, never for production validation.

## Runtime Contract

- Browser: installed Chrome first, installed Edge second, or
  `CBRS_BROWSER_EXECUTABLE_PATH`.
- Profile: `.cbrs/chrome-profile`.
- Egress mode: mandatory `client_vpn`, `client_office`, or
  `dedicated_static_isp`.
- Egress hash: expected country `CL`, with a saved hash baseline after explicit
  approval from the intended non-personal path.
- Login: manual only.
- Pacing: fixed `5.0s` minimum-safe delay by default.
- Reports: sanitized JSON under `.cbrs/logs/`.
- Stops: no retry, no identity change, no proxy fallback.

## Official Access / Allowlisting Request

Use this template when asking the client or CBRS-side contact for the preferred
production access path:

```text
We need to operate a low-volume, single-operator automation against the CBRS
commerce portal from a stable Chilean office/client network or client VPN.

The automation does not bypass login, does not solve CAPTCHA externally, does
not rotate accounts, does not rotate IPs, and stops immediately on rate-limit,
challenge, or authorization signals. It uses a normal Chrome/Edge profile with
manual login and fixed sequential pacing.

Can you confirm the recommended production access model for this workflow, and
whether the client-owned Chilean egress IP/path can be allowlisted or otherwise
recognized as the authorized operator environment?
```

## Static ISP Provider Confirmation Request

Use this only if the client cannot provide a stable Chilean office egress and a
dedicated static Chile ISP path is being evaluated:

```text
We need a dedicated Chilean egress path for one legitimate operator workflow.
The session must remain stable over multiple days, must not rotate IPs during
the browser profile lifetime, and must not be part of a shared rotating
residential pool.

Please confirm whether your service provides a dedicated/static Chile ISP
egress suitable for persistent logged-in browser sessions, whether the IP can
remain stable for at least several days, and whether it is appropriate for
accessing government/registry portals with manual login and low request volume.
```

## Incident Evidence

When a stop occurs, share only sanitized artifacts:

- `python -m cbrs preflight` output and the matching `.cbrs/logs/preflight-*.json`
- `python -m cbrs validate ...` output and `.cbrs/logs/validation-*.json`
- exact timestamp and operator action
- whether the operator was on the approved Chilean network

Never share raw cookies, credentials, JWTs, reCAPTCHA values, proxy URLs, or raw
public IPs in tickets, chat, or docs.
