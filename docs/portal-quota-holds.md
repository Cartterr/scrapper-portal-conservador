# Portal quota holds and visible search errors

The portal's visible daily-limit message overrides local remaining-credit
estimates. A `portal_quota_holds` row in the durable pool SQLite database holds
that account's searches across midnight, service maintenance and PC restarts.
It does not change accepted-search counters, browser authentication, profiles,
proxy settings or Chrome ownership. Saved search receipts may still proceed to
document/PDF work without repeating the search.

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

Implementation is in reloadable form_search/runtime_logic/runtime_observation
modules. Publish validated compatible releases at operation boundaries, preserving
the independent browser owner. Dashboard Python changes need only the dashboard
process refreshed, never a browser/owner restart. Back up SQLite before migrations.
