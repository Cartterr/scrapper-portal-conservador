# Native independent-owner rollout, September 6, 2026

- Protected environment/configuration/baseline backup and integrity-checked
  SQLite backup: `C:\ProgramData\CBRS\owner-migration-20260906`.
- Old coupled worker drained gracefully under explicit user authorization.
- External owner enabled; profiles, window mode and active ports preserved.
- Scoped failed-login replacement enabled only for `ejecutivo_2`.
- Full automated suite: 388 passed before rollout.
- Independent owner: `browser-owner-50268-bfdd253b`.
- Worker replacement: `josec-48156-4d1cb4` -> `josec-45716-4361db`.
- Chrome root PIDs 48948, 43644, 50380 and creation times remained unchanged
  across that live worker-only restart. Owner lease identity also remained unchanged.
- At that checkpoint: 3 browsers live, first/third authenticated; middle account
  still showed the rejected-login alert. Do not interpret this as 3/3 authentication.
- A real automatic owner `recover_route` command tested random candidate port
  12884. Candidate rejected with `temporary_unavailable`; candidate closed, old
  account route 10403 retained. A completed command with result `false` is not
  successful recovery. Further attempts remain governed by existing cooldowns
  and hourly limits. No quota/safety counters were reset.

This proves native worker replacement without Chrome restart, not indefinite
uptime or successful middle-account recovery. Consult live overview and receipts
for subsequent progress. Provider-side exit IP stability cannot be guaranteed.

Post-restart job `job-20260907T014943Z-c59d67f81f` completed through the external
owner: one accepted-search attempt, one consumed quota slot, and one locally
validated three-page PDF (292568 bytes). Endurance was resumed, watchdog enabled,
and the existing read-only monitor updated to distinguish owner from worker.
A second automatic middle-account candidate failed egress preflight; it was not
promoted. At handoff, recovery remains subject to its normal hourly budget and
cooldown rather than an unlimited retry loop.
