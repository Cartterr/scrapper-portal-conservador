# Runtime cleanup and Linux direction

The requested hosting target is Linux/WSL. This audit does not migrate the live
service, which still runs natively on Windows with an independent Chrome owner.

Removed:

- Unused `cloakbrowser==0.3.31` package from the repository's `.venv`.
- Unused DataImpulse administrative email/password placeholders from both root
  and native environment examples. Neither live env contained these keys.
- Obsolete optional GoLogin/Dolphin installation guidance from prerequisites.

The root `.env.example` now uses Linux paths. The native template remains for
the currently active Windows installer. No Replit integration was found.

Preserved because actively used:

- `.venv`, native Python, Google Chrome and Playwright.
- Windows worker, browser-owner, dashboard, watchdog and backup task helpers.
- Restic, its environment settings and backup/restore tooling.
- Configured proxy and CAPTCHA providers and shared Python dependencies.

Deferred source removal: the dormant Cloak adapter is imported by browser core
and represented in the settings contract. Removing that code requires a tested
core refactor/migration; changing it during this sweep would invalidate the live
release fingerprint. No core files or requirements were changed by this audit.
Do not delete all disabled features, tests, migration helpers or historical
receipts merely because they are not executing at this moment.

The populated local and protected env files had no Cloak/GoLogin/Replit keys.
Their active paths, credentials and settings were left unchanged.
