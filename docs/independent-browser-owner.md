# Independent Chrome ownership

## Architecture

`CBRS_BROWSER_OWNER_MODE=external` assigns Chrome to a separate native
`python -m cbrs.browser_owner run` process. Workers use a version-1 SQLite command
queue with no close, logout, cookie-clear, arbitrary JavaScript or proxy-change
operation. Worker shutdown only detaches. Owner observations and previews
continue without a worker. Existing installations default to `embedded`;
source changes alone do not migrate a running worker.

| Change | Activation | Chrome impact |
| --- | --- | --- |
| Overview HTML | Refresh overview | None |
| Allowlisted worker/PDF logic | Publish worker release between operations | None |
| Allowlisted form/observation/error logic | Publish owner release between operations | None |
| Worker core compatible with IPC/database | Drain and replace worker | Owner/Chrome remain alive |
| Owner internals, launch identity, incompatible protocol/schema | Explicit controlled migration | May require browser restart |

This is not arbitrary safe hot reload. Each operation pins its generation.
Never change shared schemas incompatibly or upgrade the owner's environment
in place. Use separate environments for independently upgraded dependencies.

## Durable search safety

Commands require a current worker lease and configured account. Search inputs
must match the saved job and require a quota reservation. The owner commits
accepted results before replying, so worker death cannot lose that receipt.
PDF processing resumes from saved results without repeating the search.
An owner-operation lease protects in-flight searches from abandoned-job recovery
and timeout quota release. Stale queued commands are cancelled; interrupted
running commands become uncertain and are never automatically replayed.
An OS process-lifetime lock prevents duplicate owners despite expired heartbeats.

The queue contains private queries/results/tickets, not credentials. Restrict its
directory to the authorized Windows user, SYSTEM and Administrators. Local code
releases remain trusted executable code, not a sandbox; no network RPC/upload
endpoint exists.

## One-time native migration

1. Test first. Back up protected env, pool config, baselines and SQLite via its
   backup API. Obtain explicit authorization to drain the old embedded worker;
   its existing browsers cannot be reparented into the new owner.
2. After that worker and its browsers exit, set `CBRS_BROWSER_OWNER_MODE=external`
   in protected and local env. Preserve profiles, ports, credentials and window
   mode. Provider-side expiry means unchanged ports cannot guarantee unchanged IPs.
3. Run `deploy/windows/Manage-CbrsBrowserOwner.ps1 -Action Install`, then `-Action Start`.
   The helper protects the queue ACL and registers a separate interactive-user
   owner task without a runtime limit. It refuses to launch over surviving
   coupled browsers. Owner starts before worker.
4. Check `-Action Status`, `/api/status` owner mode/lease, protected forms, unique
   profiles/routes and Chrome PIDs/start times.
5. Record owner/PIDs; run `-Action RestartWorker`. Verify a new worker lease with
   unchanged owner/PIDs, continued previews and protected forms. Validate a
   normal queued job and its local PDF before declaring production migrated.

The restart helper drains without force-killing. A failed drain leaves the worker
task disabled and surviving processes intact for inspection. Watchdog never
kills a living owner/worker for stale heartbeat. Automatic worker start requires
a healthy owner. Missing owner with surviving browsers requires an operator.

## Incremental maintenance

- `Update-CbrsRuntime.ps1 -Target worker -Action publish` for worker releases.
- `Update-CbrsRuntime.ps1 -Target owner -Action publish` for owner policy releases.
- Read the matching `-Target ... -Action status`: publication is not activation.
- `Manage-CbrsBrowserOwner.ps1 -Action RestartWorker` for worker core changes.
- External-mode `Stop-CbrsNative.ps1` pauses endurance and drains only worker;
  owner/Chrome/dashboard remain available. Owner closure requires the separate,
  explicitly authorized `python -m cbrs.browser_owner stop` through protected env.
- External-mode recovery uses a bounded `recover_route` owner command, not raw
  proxy/close RPC. Only explicitly scoped accounts with a freshly rechecked
  visible rejected-login form qualify. The owner enforces existing rate limits,
  candidate validation and exact-context adoption; worker adapters detach and
  reacquire the new binding after promotion. Other live bindings remain pinned.
- Incompatible old releases remain rejected; select `builtin` for migration.

## Evidence limits

Tests use real native Chrome in a separate owner process, terminate/replace two
test workers, and verify unchanged PID/cookies/storage plus observations while
workers are absent. Other tests cover receipts, reservations, stale commands,
timeouts, destination constraints and owner locks. This is not evidence of live
production migration, all-account acceptance or indefinite uptime. Record those
separately; never present pending source as deployed.
