# Portal quota holds and visible search errors

## Local capacity: first-success 24-hour windows

Local service capacity uses per-account fixed 24-hour windows, reconstructed
from durable accepted-search receipts. The first acceptance starts the window;
later successes in that window do not move its expiry. After expiry the next
acceptance starts a new window. Midnight has no effect. Arithmetic is UTC, display
is Chile local time, and in-flight quota reservations reduce admission capacity.
PDF completion is not a second search. Failed attempts do not count as successes.
Calendar usage records are retained for historical reporting, not admission.
This is the service's configured policy, not a claim about CBRS reset semantics.
Portal exhaustion holds continue to override local available capacity separately.

An explicit login gate is authentication evidence, never quota exhaustion.
The passive policy accepts both `/login?nextUrl=...` and historical `/login/...`
links with the complete visible login card. FNA checks before admission and
again on a pre-submission form failure, including delayed SPA rendering after
refresh. Such failures end the attempt as `auth_required`, release its local
reservation, and try another account for the same job. That job does not select
the expired account again; the normal bounded background login loop handles it.
If a due quota probe only refreshed into a login gate, its prior check deadline
is restored without deleting the historical quota evidence.

Owner replies can precede operation-lease release. At the next job cycle,
terminal owner receipts settle leftover running attempts only after the lease
has gone. Genuine unknown search outcomes remain distinguished from explicit
authentication failures. The overview labels historical quota evidence with its
detection time and presents a visible login gate as login pending.

The portal's visible daily-limit message overrides local remaining-credit
estimates. A `portal_quota_holds` row in the durable pool SQLite database holds
that account's searches across midnight, service maintenance and PC restarts.
It does not change accepted-search counters, browser authentication, profiles,
proxy settings or Chrome ownership. Held accounts are excluded from all job
operations until their deadline. Saved search receipts remain intact so document
work can resume later without repeating the search.

Open, visible HeadlessUI dialogs are identified by their heading, message and
Close button, not their generated numeric IDs. A generic refresh message allows
one same-tab refresh per job. Automatic re-submission requires a definitively
rejected matching search response and the visible generic dialog. Timeouts and
ambiguous outcomes are not replayed. Evidence is captured before refreshing.
A daily-limit dialog or classified daily-limit response creates a hold instead.

The first successful job completion in the preceding 24 hours plus 24 hours is
a conservative **estimated next check**, not a confirmed CBRS reset schedule.
If no receipt exists, use detection plus 24 hours. The overview labels this
uncertainty and keeps available credit at zero until an accepted query proves
access. Repeated passive observations never extend the deadline. At eligibility,
one query is admitted atomically; a further failure retains a one-hour interval
before another probe. A successful result clears the hold.

Manual portal usage is not necessarily represented in the local counter. Do not
fabricate 20 successful searches just because the portal reports exhaustion.
User-reported limits must retain `user_reported_portal_limit` evidence provenance.

An unknown search outcome ends only that job as `failed` with its original
`search_outcome_unknown` / `search_receipt_incomplete` code. The overview labels
this as review required, not no capacity. Evidence stays intact; no success or
PDF is fabricated and the query is not replayed. An active browser-operation
lease prevents premature finalization. Endurance can enqueue the next fixture
after its normal cooldown. A saved successful search instead resumes document
work under its original account without another search or quota charge.
If that account is unavailable after retrieval failure, finalize the endurance
slot with `document_retrieval_deferred`; keep the receipt/items for later review
and document-only recovery. Never count this as a finished PDF. This prevents
one document failure from starving other eligible accounts.

Implementation is in reloadable form_search/runtime_logic/runtime_observation
modules. Publish validated compatible releases at operation boundaries, preserving
the independent browser owner. Dashboard Python changes need only the dashboard
process refreshed, never a browser/owner restart. Back up SQLite before migrations.
