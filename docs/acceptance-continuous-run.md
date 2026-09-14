# Continuous acceptance run

Started 2026-09-13 21:21:32 America/Santiago. First 24-hour boundary:
2026-09-14 21:21:32. Observation continues indefinitely after that boundary.

- Overview: http://127.0.0.1:8765/
- Acceptance page and machine-readable evidence: http://127.0.0.1:8776/ and `/status.json`.
- Independent unit: `cbrs-acceptance-monitor.service`, enabled at boot.
- Source: `/opt/cbrs-acceptance-runner`; uses the existing Linux Python environment.
- Evidence: `/opt/scrapper-portal-conservador/.cbrs/runtime/acceptance/`:
  `run.json`, `samples.jsonl`, `evidence.json`, `status.json`, `resultados.csv`, `pdf/`.
- Samples every minute; read-only Codex follow-up every 15 minutes, quiet unless
  a criterion passes, the first 24-hour verdict arrives, or action is needed.

The workload is the 32 unique supplied FNA tuples in `examples/acceptance-live.csv`.
It submits full-document jobs once and then observes the same durable IDs. Cached
cases do not prove new processing. No force/replay of ambiguous searches, quota
override, browser restart, proxy change, or legacy repeating endurance loop.

The new client is staged over the existing backend at
`c059762cbcf0bda90ff3493245dfd234f6e328da`. This does not activate or certify the
new backend. Owner PID 384 and worker PID 429 were unchanged at launch.

A16 requires 24 hours, at least 98% minute-sample coverage, no gap above 180s,
unchanged boot/owner/worker identity, active services, no orphan Chrome, new
completed jobs and observed idle time. The last-hour median RSS must not exceed
the larger of 125% of the 30–90 minute baseline or baseline + 256 MiB. These are
explicit operational thresholds, not thresholds specified by the client.

Elapsed time never approves other criteria. A2 needs visual correspondence;
A4/A7/A8/A9 need a genuinely confirmed nonexistent inscription, not an invented
result. A10 requires all accounts actually portal-held and intact pending-quota
rows with resume times; 32 fixtures may not exhaust quota. A11 requires those
same jobs to complete without resubmission. A1/A6/A12–15/A19 remain deferred to
an authorized isolated scenario. A17/A18 remain pending unless separately
verified; local unit tests do not certify a clean host with no network.

To pause only this observer, stop `cbrs-acceptance-monitor`. That does not cancel
already queued jobs or stop the worker, owner, Chrome or dashboard. Pausing the
Codex automation alone also does not stop the observer. Never stop protected
runtime components to maintain this runner. PC sleep/shutdown interrupts evidence.
