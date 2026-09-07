# Non-negotiable CBRS browser preservation rules

## Explicit failed-login replacement exception

The user authorized replacement/cleanup for the failing middle account only.
Use `CBRS_FAILED_LOGIN_REPLACEMENT_ACCOUNTS` with explicit account IDs; never
enable a wildcard or expand it to healthy siblings. The complete visible login
form plus the specified rejection alert is required; unknown DOM, an HTTP 400
alone, or an authenticated search error is not permission. Keep the failed
context until a replacement proves the protected form, then close only that
failed context. Keep the exact successful candidate alive. Existing rotation
budgets, uniqueness checks, country checks and terminal-error stops still apply.
This exception does not authorize stopping the shared worker or healthy browsers.

## Independent-owner maintenance

`CBRS_BROWSER_OWNER_MODE=external` is an explicit opt-in, not evidence of a
completed migration. Verify the separate browser-owner lease and Chrome PIDs.
Once deployed, worker maintenance must detach via the local command adapter;
it must never signal or kill the owner, clear its state, or change live routes.
Use `Manage-CbrsBrowserOwner.ps1 -Action RestartWorker` for compatible worker
core changes and `Update-CbrsRuntime.ps1 -Target worker|owner` for allowlisted
releases. Maintain IPC/schema compatibility and preserve accepted search receipts.
Owner-core/launch changes still require an explicitly authorized migration.
Never activate external mode over a surviving coupled worker/browser. See
`docs/independent-browser-owner.md` for boundaries and validation gates.

## Supported live application updates

After the one-time adapter migration, use `deploy/windows/Update-CbrsRuntime.ps1`
for the allowlisted components described in `docs/runtime-live-updates.md`.
Publish tested immutable releases; confirm activation and unchanged owner/browser
PIDs. Do not restart the worker for these updates. Roll back code only at a safe
boundary, never quotas, completed jobs, cookies or browser state. UI-only edits
belong in `cbrs/web/overview.html`; refresh only the overview.
Core/schema/dependency/browser-owner changes remain controlled migrations and
must not be advertised as arbitrarily hot-reloadable. Never bypass the core hash
check, inject code into an old worker, or use `importlib.reload` on live owners.

## Latest override: authenticated sessions survive service maintenance

A service stop/restart request is NOT permission to close, log out, replace, or
change the proxy of any authenticated browser, including previously authenticated
contexts with temporarily unknown DOM. Preserve their existing worker owner too
while ownership is coupled to Chrome. Never invoke a shutdown path that closes
those contexts. Do not promise preservation across a PC shutdown, provider-side
IP expiry, or a crash. Configuration backups are not backups of live sessions.
Until a tested independent browser-owner/handoff architecture exists, defer any
worker restart or code activation that would destroy them. This override takes
precedence over every older whole-service-stop exception below and in runbooks.

## Supported service browser

Use regular Google Chrome through Playwright for the worker and all recovery.
Do not introduce GoLogin/Orbita, Dolphin, anti-detect, or alternate-browser
fallbacks. Preserve the configured headed/headless choice; a request to use
regular Chrome alone does not authorize restarting contexts or changing that
choice. Successful diagnostic windows are not worker-owned sessions unless
explicitly adopted without closing them. Never claim they are already deployed.

## Recovery refinement (latest user direction)

Failed accounts may try new disposable Mobile proxy candidates under the
existing worker, with distinct profiles and bounded traffic. NEVER close a
proven candidate to reopen it: adopt that exact live authenticated context.
Keep any older production context open but inactive until whole-service stop.
Only newly created candidates that never authenticated may be closed after a
failed test. Once any context for an account has authenticated, do not rotate
that account automatically, including after a later unknown DOM check.
This refinement permits selecting a successful new context for a failed account;
it does not permit worker restarts or closing any existing production browser.

These rules override older repository runbooks that suggest restarting workers
or browsers to apply a fix or recover a proxy. Apply them to every maintenance,
debugging, testing, installation, upgrade, and deployment task in this repository.

- NEVER stop, restart, kill, replace, or close an existing production Chrome
  instance, its Playwright context, its worker owner, or its process tree during
  ordinary operation. A successful authenticated browser session is expensive
  operational state, not a disposable test process.
- Preserve every running account browser until the PC reboots/shuts down or the
  user explicitly requests stopping/restarting the ENTIRE service. Requests to
  fix, test, deploy, commit, push, update documentation, recover an account, or
  change a proxy do NOT authorize a worker/service/browser restart.
- NEVER restart a worker just to load changed Python code. Implement and test
  changes offline, leave them pending activation, and report that fact. Activate
  them at the next user-authorized whole-service restart or PC reboot.
- NEVER close a healthy account while recovering a different account. Never
  terminate all Chrome/python processes or use taskkill against the worker tree
  as a diagnostic shortcut. Do not stop scheduled worker tasks for deployment.
- NEVER clear cookies, storage, cache, profiles, or auth state in an existing
  production instance. Persisted cookies do not guarantee that reopening Chrome
  will restore the accepted browser/IP/reCAPTCHA context.
- HTTP 400, temporary_unavailable, CAPTCHA errors, unknown DOM, idle time,
  cooldown, a stale lease, and a failed health probe do NOT authorize closure,
  replacement, logout, proxy reassignment, or a process restart.
- Once a protected authenticated form has been observed in a context, preserve
  that fact for lifecycle protection even if a later DOM check is unknown.
  Unknown alone must not trigger destructive recovery or an automatic reload of
  that previously authenticated context.
- A watchdog must alert on an unhealthy living worker and retain it. It may
  start an absent worker only when no previous owner/browser survives. It must
  respect tasks disabled by an explicit service stop.
- Keep passive previews and diagnostics available. When an operation requires
  closing/replacing a live browser, defer it and explain the preservation rule.
  Do not report pending source changes as loaded in the current worker.
- Work natively on Windows, preserve unrelated edits, and never expose secrets.
