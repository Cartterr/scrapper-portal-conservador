# Repository-local runtime

The repository location is discovered from the code, not the shell's working
directory. No runtime directory variables belong in `.env` or its examples.
`cbrs.paths` supplies the internal environment needed by Chrome, Playwright,
Restic and subprocesses. Production entry points disregard stale directory
variables inherited from older installations. Explicit Settings/test injections
remain available for isolated diagnostics.

All application state is under `.cbrs/runtime/`:

- `accounts/`: isolated persistent Chrome profiles and egress baselines.
- `pool/`, `browser-owner/`: databases, leases, command queue and releases.
- `outputs/`, `logs/`: PDFs, evidence and application logs.
- `tmp/`, `cache/`, `config/`, `data/`, `state/`: process-local temporary/XDG data.
- `backup/restic/`, `backup/snapshot/`: encrypted backups and SQLite snapshots.
- `secrets/restic-password`, `bin/restic.exe` (Windows): backup key and helper.

The root `.env`, `.cbrs/`, temporary work, and Python environments are ignored
by Git. The runtime and rollback directories require private filesystem access.
Do not commit profiles, databases, keys, environment files or generated PDFs.

Windows task helpers now discover the repository, use its `.env`, and need no
directory arguments. Existing scheduled actions must be migrated too; changing
a script default does not rewrite an already registered action. Linux service
templates use `@REPO_ROOT@`; `deploy/install-ubuntu.sh` renders them automatically
with `deploy/render_units.py`. Do not install the unrendered templates directly.
Linux deployment changes require Linux validation during the separate migration.

The active service now runs in native Linux inside WSL. It retains headed Chrome
on Xvfb, with no Windows browser windows. See `linux-service.md` for operation.
Chrome and Python installations, OS service registrations and operating-system
event logs are system infrastructure, not relocated application data.

An encrypted backup in the same repository is useful for recovery from logical
damage, but does not protect against loss of that drive or deletion of the entire
repository. Preserve an independent disaster-recovery copy if that is required.

The prior external runtime and protected environment were retained for rollback.
Never start those old launch configurations while the new owner is alive. Never
restore an older queue or quota database over newer accepted searches. Moving
live profiles requires an authorized shutdown and an integrity-checked copy;
copied cookies alone do not guarantee authenticated sessions will survive.
