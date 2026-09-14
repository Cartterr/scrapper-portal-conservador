# Portal error dialog: a compromised proxy exit

## Evidence

The operator ran the same account and the same search from a clean network and
it succeeded, while every pooled exit showed this dialog:

- Container: `[id^="headlessui-dialog-panel-"][data-headlessui-state~="open"]`
  or `[role="dialog"]`; the generated numeric ID is never hardcoded.
- Visible `h2`/`h3` with normalized text `Atención`.
- Visible `p` with `Se ha detectado un problema, refresque la página e intente
  nuevamente.`
- Visible `button` with normalized text `Cerrar`.

Whitespace and accents are normalized; hidden dialogs and other messages do not
qualify. The daily-quota message is a different dialog with its own meaning and
still takes priority.

## Verdict

That exact visible signature is treated as evidence about the ROUTE, not the
account: `verdict: proxy_compromised`. It is not evidence of invalid
credentials, of a portal-wide outage, or of an expired session.

## Reaction

1. **Detect, continuously.** `portal_dialog_evidence()` recognizes the dialog
   during a search and on any living page through the passive DOM sampler, so
   an idle authenticated browser is flagged without waiting for a query.
2. **Never refresh and retry.** The previous bounded reload plus same-search
   retry is gone. The dialog ends the attempt with no replay and no second
   submission, so the account pays no search quota.
3. **Quarantine the exit.** `account_proxy_routes.compromised_since` is a
   durable flag, separate from the rotation status, so a failed candidate
   cannot silently clear it. While set, the account is never opened, logged
   into, searched through, or reconciled.
4. **Hand the search over first.** The attempt ends with `retry_account`, so an
   already authenticated sibling runs the same query immediately in the same
   worker pass. The compromised account pays no search quota.
5. **Ditch the browser.** On the next worker pass `ditch_compromised()` logs
   out, clears cookies and web storage, closes every context of that account
   (current and retained) and deletes their Chrome profile directories.
   Nothing from the compromised exit is carried into the replacement.
6. **Replace the exit.** The existing DataImpulse candidate machinery proves a
   new sticky port: preflight, proxy health, country, egress uniqueness and a
   real authenticated protected form. Only an adopted replacement clears the
   quarantine and returns the account to the pool.
7. **Pace the replacement.** The browser owner runs one operation at a time, so
   recovery takes one account and one candidate per worker pass, at most once
   every `COMPROMISED_RECOVERY_INTERVAL_SECONDS` (60s), oldest attempt first.
   Queued searches keep their turn instead of waiting behind a candidate sweep.
8. **Keep searching.** If every account is quarantined, the job waits in
   `waiting_capacity` until the first replacement authenticates. The shared
   `temporary_unavailable_all_accounts` outage circuit is deliberately not
   opened by this verdict, and the pool publishes no quota-reset countdown:
   recovery, not tomorrow's quota, is what it is waiting for.
9. **Retry without a long wait.** A failed recovery leaves the account paused
   with `proxy_compromised` and no `resume_at`: only a proven replacement
   releases it, and every pass retries at the route's own pacing (candidate
   retry delay, rotation cooldown, hourly allowance). The same dialog on a
   replacement exit repeats this process instead of waiting out a 24-hour
   window. A worker restart re-publishes the hold from the durable quarantine,
   so a compromised exit is never advertised as ready.

## Boundaries

Ditching is the single authorized destructive browser path and applies only to
an account whose exit produced this exact dialog. Healthy siblings, their
contexts and their profiles are untouched. Quota reservations are released, not
consumed; accepted search receipts are never cleared or replayed. Rotation
budgets, uniqueness checks, country checks and terminal-provider stops all
still apply, so recovery cannot loop unbounded.

## Cross-process behavior

With the independent browser owner, detection can happen on either side. The
owner records the quarantine at the point of detection and returns
`route_compromised` in its durable command reply; the worker rebuilds the same
verdict and drives recovery. `ensure` and `recover_route` both refuse to
authenticate through a quarantined exit, and `recover_route` ditches the
owner's own Chrome before proving a candidate.
# Recovery fairness after repeated detections

Recovery ordering uses `account_proxy_routes.last_recovery_attempt_at`, not
`updated_at`. Repeated observations may update route metadata but cannot move
an account behind its siblings. Eligible accounts with no attempt in the current
quarantine episode are served first, then least-recently-attempted accounts.
Existing per-route cooldowns and rotation limits still apply.

The nullable column is added by the normal JobStore migration. A timestamp older
than the current quarantine episode is treated as unattempted, allowing an
already-running browser owner to retain the previous writer implementation.
Activation needs worker-only maintenance, not a browser-owner restart.
Regression coverage: `tests/test_recovery_fairness.py`.
