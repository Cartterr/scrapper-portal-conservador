# Durable document recovery

Accepted search results and private document tickets remain in SQLite. The
document stage does not repeat the search or consume another search quota.
Validated page images and a private manifest are retained under each job's
`.staging/<item_id>` directory. These contain private references: do not publish,
commit or expose this directory through the dashboard. Include it in protected
backups; no automatic retention deletion is currently configured.

PDF assembly uses a temporary output and validates the PDF before atomic
publication. A restart can register an already published PDF or rebuild from
cached pages without contacting the portal. Missing pages still require the
original account, subject to its cooldown and quota hold. A search receipt alone
cannot reconstruct document images that were never downloaded.

The endurance scheduler revives deferred document jobs after 120 seconds and
does not enqueue the next search while one remains pending. Document recovery
continues while new endurance searches are paused. After three automatic retries
(including persisted historical retries), it finishes with the documented error
`document_recovery_exhausted`, never a request for manual review. Retry does not
guarantee eventual success: an expired ticket, missing content or portal failure
cause a fake completed or empty result. Cancelled jobs are not revived. Ambiguous
search outcomes now remain pending. Once the prior browser-operation lease ends,
the runtime authorizes a sequential attempt on a different eligible account.
An account with an uncertain attempt is excluded for that job; if no alternate
is eligible the job waits. This policy can repeat a portal search whose response
was lost; it is not a guarantee of exactly-once portal execution. Accepted receipts
and live-operation leases always prevent another search. Incomplete accepted
receipts stay pending rather than being replayed. Account limits and quota holds
still apply. No unknown outcome is relabeled as an empty response.

`deploy/archive_job_history.py` removes only named terminal entries from recent
history. Detail APIs, attempts, artifacts and quota evidence are retained. Restore
visibility by removing the corresponding `archived_jobs` row in a controlled
local maintenance transaction. Archiving does not change execution outcomes.
`operator_reported_search_no_artifact` is historical operator evidence, not proof
of an empty portal result. Only an accepted empty response proves zero results.
