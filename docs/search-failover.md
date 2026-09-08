# Search failover and uncertainty

## Cause of the historical failures

The migration-validation jobs ending `e180bd6604` and `fad65f5a40` were finalized
by an orchestration policy, not by a confirmed empty portal result. Their saved
screenshots show an expired-session login form and a navigation/loading screen,
respectively. Screenshots alone cannot prove whether CBRS accepted a request.
The worker queued an alternate retry after a generic exception, then its next
pass called `finish_for_review` and finalized the job instead. The owner also
refused unresolved search outcomes, so blindly dispatching a sibling was not a
valid solution.

## Current behavior

- Arm request listeners, fill fields with read-back, then click once.
- A visible login gate or a confirmed failure before a commerce POST fails over
  without charging search quota. No submitted request within 15 seconds is a
  pre-submit failure, not an indefinitely stalled form.
- After a possibly submitted request, preserve the uncertain attempt. Wait for
  queued/running owner commands and the operation lease to finish. A durable
  clearance then permits another account, never the same uncertain account.
- Uncertain attempts retain conservative quota reservations for 24 hours. A
  second search on a different account may also consume quota; it is not an
  assertion that the first request never reached CBRS.
- Accepted results (including an empty list) remain authoritative. Saved or
  incomplete acceptance receipts are never cleared to obtain another search.
- Try eligible alternate accounts in the same worker pass. If none can proceed,
  keep the request pending rather than finalize `search_outcome_unknown`.
- Previously unconfirmed accounts remain excluded for that job. If every account
  has an uncertain attempt, operator reconciliation is required; do not loop
  indefinitely or forge success. Other queued jobs can continue.

Successful sessions and proxy bindings are independent of job failover. No
browser closure, logout or proxy rotation is part of this search retry flow.
Authentication checks now accept a currently visible protected form without
refreshing cookies or navigating away. On startup, a cookie-backed protected
page is rendered and checked before attempting a refresh API request. This
avoids unnecessary refresh failures being reported as authentication outages.

During live recovery both historical jobs obtained accepted results on sibling
accounts, exposing a second bug: document authentication unconditionally called
the refresh endpoint, which returned 401 despite the native portal's successful
search. The document client now reuses its browser's scoped access cookie when
its token has more than 60 seconds remaining. Missing/malformed/expired tokens
still require refresh; tokens never appear in diagnostic events. Allowlisted
operation names distinguish auth refresh, ticket validation and image failures.
Long downloads also exposed expiry mid-document. Forced authentication must
actually enter the login UI rather than accept an old protected DOM. Successful
browser-origin login/refresh updates both the access cookie and native
localStorage token. Cached pages and accepted tickets survive document retries;
neither a new search nor proxy rotation is part of this recovery.

## Explicit historical repair

`deploy/resume_unconfirmed_jobs.py JOB_ID [...]` previews eligibility;
`--apply` requeues only named historical unconfirmed failures. It preserves all
attempts/evidence, checks for owner activity, and records retry authorization.
Do not apply this tool to accepted, cancelled or already completed jobs.

`deploy/resume_document_jobs.py JOB_ID [...] --apply` resumes only named deferred
document jobs with durable acceptance receipts and no live owner command. It
retains the accepted account, results and cached pages; it never clears a search
receipt or authorizes another search.

Validation: 465 tests passed, including a real Chrome
navigation failure followed by alternate-account selection and a valid two-page
PDF, all-accounts-uncertain bounds, receipt idempotency, queued/live-operation
exclusion and conservative quota accounting. The overview's JavaScript also
passed a syntax check.

Live validation on 2026-09-08 completed both named historical jobs through
alternate accounts. Their complete PDFs contain 6 and 44 pages, respectively;
HTTP downloads, SHA-256 digests, independent Poppler page counts and rendered
pages were checked. The 44-page job resumed cached pages after authentication
expiry without replaying its accepted search. All three sticky proxy ports and
route generations were preserved across the explicitly authorized maintenance.
Portal login rejection on an individual route remains an external condition;
passing regression tests is not a guarantee of continuous CBRS availability.
