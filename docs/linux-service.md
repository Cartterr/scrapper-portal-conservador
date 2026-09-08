# Linux/WSL service

## Migration validation (2026-09-08)

### Preservation upgrade

Candidate adoption now retains every previous production context, regardless
of earlier authentication or later login rejection. The worker requires a
capability marker tied to the current owner lease before sending candidate
recovery commands. Older owners receive ordinary same-browser login retries,
but no candidate commands that could invoke their obsolete cleanup exception.
The worker-side guard is deployable without closing Chrome. The owner-side
change was activated by the user-authorized owner restart on 2026-09-08; never
forge its capability marker or hot-patch a living owner to force activation.

Recovery is scoped to `ejecutivo_2,ejecutivo_3`, using regular headed Chrome and
the existing Chile-targeted DataImpulse mobile credentials. Sticky candidates
are uniformly sampled with `secrets.choice` across ports 10000 through 20000,
excluding active/pending and previously rejected ports. Exit identity and
country checks still apply: a new port does not guarantee a new IP. Current
limits are three candidates per recovery, ten seconds between portal-rejected
candidates, and 30 attempts per account per hour. Existing global cooldowns,
terminal stops and quotas are not reset by configuration changes.

Live verification after activation: ejecutivo_2 sampled 18838, 17043 and 18059;
all reached login testing and returned portal rejection. ejecutivo_3 then began
validating 14729. This verifies fresh candidate selection for both scoped
accounts, not successful recovery. The completed regression suite passed 450
tests. Passive rejection checks now also inspect known LOGIN_GATE states,
avoiding unnecessary same-route submissions when a rejection is already visible.

The Linux stack is live, but full account parity is still pending: one account
completed a new search and PDF download; two accounts are still receiving the
portal's generic login rejection and remain in bounded recovery. This is not
evidence of incorrect passwords. Do not broaden proxy replacement permissions,
reset quotas, or replay searches with unknown outcomes to hide this gap.

The successful live validation job is `job-20260908T025714Z-8a25f803a9`
(one result, three-page PDF). Backup restore verification passed with 45 PDFs.
A worker-only restart preserved the independent owner and all Chrome PIDs.
Windows logon bootstrap is installed; a full host reboot has not been tested.

The authoritative runtime checkout is `/opt/scrapper-portal-conservador` inside
`Ubuntu-24.04`, on its native Linux filesystem (not `/mnt/v`). Its `.venv`,
`.env`, and `.cbrs/runtime/` are local to that checkout. Windows copies are
offline rollback material and must never run alongside it.

## Services and behavior

- `cbrs-browser-owner`: independent owner of regular sandboxed Google Chrome.
- `cbrs-worker`: durable queue, account selection, recovery and search workflow.
- `cbrs-dashboard`: loopback overview/API on port 8765.
- `cbrs-display`, `cbrs-x11vnc`, `cbrs-novnc`: private virtual display and
  loopback browser recovery on port 6080. Chrome remains headed for behavior
  parity; it has no native Windows desktop windows.
- `cbrs-backup.timer`: encrypted repo-local backups.
- `cbrs-watchdog.timer`: non-destructive worker recovery; never restarts Chrome.
- `cbrs-worker-resume.path`, `cbrs-configuration-apply.path`: overview controls.

Login recovery, account quotas, proxy isolation, CAPTCHA provider configuration,
job idempotency, PDFs, error evidence and live previews use the existing Python
implementation, not a second scraper. External browser ownership remains
mandatory (`CBRS_BROWSER_OWNER_MODE=external`). No proxy bypass or browser
sandbox disabling is introduced by the migration.

## Operation

Run from a Linux terminal in the active checkout:

```sh
sudo systemctl status cbrs-browser-owner cbrs-worker cbrs-dashboard
sudo systemctl restart cbrs-worker
.venv/bin/python -m cbrs jobs backup
.venv/bin/python -m cbrs jobs backup-verify --require-pdf
.venv/bin/python deploy/linux_health.py
```

Worker restart must leave the independent owner and Chrome PIDs unchanged.
An explicit live verification is available with
`sudo .venv/bin/python deploy/verify_worker_restart.py`; it restarts only the
worker and compares the owner and production Chrome PIDs before and after.
Owner/display shutdown requires explicit authorization and can require fresh
logins. The owner intentionally does not crash-loop/restart automatically.
Startup stale-lease recovery first checks the owner lock and surviving process
arguments. Living unhealthy browsers require inspection, not forced recovery.

Service output is in `.cbrs/runtime/logs/services.log`; all application data
stays under the checkout. Systemd unit files and installed OS packages remain
normal Linux system infrastructure. Backups on the same disk are not protection
against loss of the whole disk.

## Host startup and deployment

`deploy/install-wsl-host.ps1` installs the sole Windows bootstrap task,
`CBRS WSL Host`. At user logon it starts a hidden WSL keepalive process; all
application supervision and timers are Linux systemd. WSL can shut down when
its host processes exit, so this bridge is required on this workstation.
This is logon startup, not an assurance of service before Windows sign-in.

`deploy/render_units.py` derives installed unit paths from the checkout. Do not
install unrendered templates or restore old systemd drop-ins pointing to
`/opt/cbrs` or `/var/lib/cbrs`. Those legacy overrides were archived before use.
The Windows-to-Linux state migration copied data offline, rewrote stored paths,
and checked SQLite integrity. Do not re-run migration against live databases.

Do not copy old Windows queues over newly accepted Linux searches during a
rollback. Stop Linux first, preserve its newest state, and perform an explicit
reverse migration before considering the disabled Windows launchers.
