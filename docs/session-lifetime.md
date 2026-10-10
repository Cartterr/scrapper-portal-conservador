# Portal session lifetime: what keeps a login alive

Evidence gathered 2026-10-10 from the WSL production runtime without any
portal traffic: the per-account request logs
(`.cbrs/runtime/logs/requests/<account>/*.jsonl`, 2026-09-25 to 09-29), the
Chrome profiles (cookie store, `localStorage`, HTTP cache) and the job events.
Identity values were never read; only names, timestamps and claims.

## The two tokens

| Token | Where | Lifetime | Who renews it |
| --- | --- | --- | --- |
| Access token `auth_cbrs_token` | JWT (`RS256`, `type: access`) in `localStorage` and in a cookie | 30 minutes (`exp - iat = 1800` on every token found) | The SPA, only when it needs it (below) |
| Refresh token `cbrs_refresh_token` | httpOnly, Secure cookie | 7 days from issue, **re-issued on every refresh** | The server, on `POST /api/v1/auth/refresh` |

`auth_cbrs_stay_signed_in` (14 days) only tells the SPA to keep the access
token in a persistent cookie; it does not change the server contract. The
`incap_*` cookies belong to the Imperva WAF in front of the portal and play
no role in authentication.

## How the SPA uses them (from the cached bundle `index-*.js`)

- On boot: if the stored access token is expired or within 30 s of expiry,
  `POST /api/v1/auth/refresh` with credentials; on failure, clear local auth.
- Before any request that carries `Authorization: Bearer`: same check, same
  refresh, single-flight. On failure: clear local auth and navigate to
  `/login/sesion-expirada`.
- After a 401 (or a 403 on `/api/`): one refresh and one retry; on failure the
  same redirect.
- There is **no timer**. An idle tab never talks to the server.

So "the page still shows the form but the first search after hours reloads
and logs me out" is the redirect above: the access token had expired (always,
after 30 minutes) **and the refresh was rejected**. Expiry alone is renewed
transparently.

## What the logs show about the refresh token

Active profiles, 2026-09-27 to 09-29 (UTC):

| Account | Chain started | Refreshes seen | Outcome |
| --- | --- | --- | --- |
| ejecutivo_3 | login 09-14 02:30 | 09-27 20:25, 09-28 09:52, then **27.3 h idle**, 09-29 13:12 | all `200`; chain alive after 15 days and an egress IP change |
| ejecutivo_2 | login 09-14 03:44 | 09-27 20:24, 09-28 09:52, 09-28 12:56, then 22.2 h idle, 09-29 11:09 | `401` on 09-29 with **no request of any kind** in between; login required |
| ejecutivo_1 | login 09-21 00:39 | 09-27 20:24, 09-28 09:52, 09-28 12:56, 09-28 15:58 | `401` three hours after a `200`, with no request in between; `401` every hour afterwards; login on 09-29 11:09 |

Facts that follow:

1. The refresh cookie is sliding: chains outlived the 7-day cookie by a week
   because every refresh issued a new one.
2. Idleness does not kill it within at least 27 hours, and an egress IP change
   between two refreshes does not either (ejecutivo_3 on 09-29, ejecutivo_1 on
   09-28 12:56).
3. Two chains were nevertheless revoked server-side on 09-28 between 12:56 and
   15:58 UTC with zero requests from our side: no logout (none in any profile,
   ever), no login elsewhere, no Chrome restart in between. The one chain
   that had not refreshed at 12:56 survived. The cause is on the portal
   (a revocation, a purge or a rollback of tokens issued around that time);
   it cannot be prevented from here, only noticed early.

## Consequences for the service

- **Renew the access token before it expires, from the live page.** That is
  the same request the SPA makes, with the same cookie, so the server sees a
  normal client. It keeps the refresh cookie rotating and means a search never
  starts with an expired token. Implemented in
  `_PersistentAccountBrowsers._keepalive` with lead time
  `CBRS_SESSION_KEEPALIVE_SECONDS` (default 300 s, so about one request every
  25 minutes per authenticated account; 0 disables). Never a login, never a
  navigation, never a reload; skipped when the page already needs re-login.
- **Navigating routes does not help.** SPA routes are client-side; the server
  only sees API calls, and only the refresh extends the session. Extra page
  loads add WAF and reCAPTCHA surface for nothing.
- **A rejected refresh is a dead session.** The `session_keepalive` event
  carries `http_status`, `refresh_cookie_present`, `refresh_cookie_rotated`
  and `remaining_seconds`. A `401` there means the next search would have hit
  `/login/sesion-expirada`; the existing login path handles it at job time and
  the event timestamps build the record needed to show the portal when its
  tokens die.
- **Preservation rules are unchanged.** The keep-alive touches no cookie the
  portal did not issue, closes nothing and reloads nothing.

## Open observation

On 09-28 14:58 ejecutivo_1 loaded the protected page and the SPA made no
request at all afterwards (no boot calls, no reCAPTCHA), unlike every other
page load. One hour later its refresh was rejected. Whether that document was
the SPA or a WAF interstitial is not recorded; the request log stores status
and type, not bodies.
