# Live application updates without closing Chrome

## Architecture and boundary

For independent worker replacement, see [Independent Chrome ownership](independent-browser-owner.md).
External mode loads separate worker/owner releases; select `-Target worker` or
`-Target owner`. The same-process description below applies to embedded mode.

The native worker remains the stable browser owner and exclusive lease holder.
It retains Playwright, contexts, profiles, proxy settings and lifecycle latches.
Application behavior is loaded as immutable versioned modules on that same owning
thread. This is **not** a separate browser broker, process-handoff system, Python
monkey-patching, or arbitrary safe hot reload.

Reloadable components (ABI 1):

- `runtime_logic.py`: job processing, using the durable core store and quotas.
- `runtime_observation.py`: passive DOM evidence and preview interval policy.
- `form_search.py`: the form-driven FNA operation.
- `pdf.py`: local PDF construction.
- `error_evidence.py`: attempt error capture.
- `web/overview.html`: UI markup/styles/script, served on the next page refresh.

The worker checks for requested releases **between operations**. An ongoing
search/download finishes with its original generation. Browser preview callbacks
inside that operation use the same pinned generation. Queued work uses the new
generation after activation. A long-running operation delays activation; do not
kill it or submit another search to accelerate an update.

Core changes (browser owner, launch arguments, proxy/session identity, database
schema, client/solver adapter internals, safety contracts, worker scheduler,
dependencies or Python) still require a controlled migration. Publication checks
the core fingerprint against the owner's boot fingerprint and rejects mismatches.
Adding a new reloadable component requires an ABI/core migration first.

## Native Windows workflow

1. Modify supported component files; preserve public function contracts.
2. Run the full regression suite, including `tests/test_runtime_updates.py`.
3. Publish: `deploy/windows/Update-CbrsRuntime.ps1 -Action publish`.
4. Read: `deploy/windows/Update-CbrsRuntime.ps1 -Action status`.
5. Confirm `state=active`, the requested hash, unchanged worker owner and unchanged
   browser start times/routes. The overview shows version and update status.

Default release state is `G:\CBRS\pool\runtime-updates`, beside the actual pool DB.
For another installation pass `-StateRoot` matching that DB's parent directory.
The installer runs from this checkout; retain the `cbrs/web/overview.html` asset.
No credentials, cookie exports, or `.env` copies belong in a release.

Publishing snapshots the allowlisted code, verifies syntax/contracts, hashes each
component, writes an immutable release directory, then atomically changes the
desired pointer. It does not itself prove behavioral correctness. Only trusted
local maintainers may write these executable files; use the same restricted ACL
as the application. There is deliberately no HTTP code-upload/execute endpoint.

The owner verifies checksums, core compatibility and callable contracts, loads a
candidate namespace, then switches at a safe boundary. Syntax/import/integrity
failures keep the prior generation. Errors expose bounded reason codes, not raw
exception messages. This is trusted Python code, **not a sandbox**: tests/review
must prohibit top-level side effects and browser lifecycle changes.

## Rollback and safety

`deploy/windows/Update-CbrsRuntime.ps1 -Action rollback -Release <previous hash>`
requests the previous immutable code at the next safe boundary. `builtin` selects
the boot-loaded code. Rollback never rewinds quotas, successful searches, PDFs,
cookies, or database state. It never automatically replays a failed operation.
If new code causes a business-operation failure, inspect durable evidence and
request rollback explicitly; do not manufacture an automatic retry.

A hard process crash or incompatible owner change cannot be solved by this
adapter. The existing preservation watchdog still applies. The one-time migration
to install this adapter requires an explicitly authorized service restart. Future
updates to the listed modules do not. UI edits require refreshing the overview,
not the portal or worker; save complete, validated HTML before refreshing.

## Evidence vs claims

Tests include real local Chrome surviving update/rollback with the same browser
PID, cookies and storage. This does not prove indefinite uptime, target portal
acceptance, or immunity to provider-side sticky-IP expiry. Live rollout must
separately verify owner/PIDs/routes before and after a published release.
