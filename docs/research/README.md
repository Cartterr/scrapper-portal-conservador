# CBRS Research Index

Curated research notes for long-term CBRS portal operation. These files summarize public docs, public operator reports, and repo-local probe findings without storing raw secrets, tickets, JWTs, cookies, or account data.

## Files

- [01-cbrs-portal-facts.md](01-cbrs-portal-facts.md): CBRS-specific portal facts and local probe findings.
- [02-platform-limitations.md](02-platform-limitations.md): reCAPTCHA, Imperva, browser profile, and account/session limitations.
- [03-current-system-features.md](03-current-system-features.md): Safety features currently implemented in this repo.
- [04-caveats-and-risk-signals.md](04-caveats-and-risk-signals.md): Operational caveats, stop signals, and known failure modes.
- [05-long-term-options.md](05-long-term-options.md): More permanent solution paths to investigate later.
- [sources.md](sources.md): Public sources and local evidence files used by the curated notes.

## Working Principle

The project should behave like a cautious logged-in operator using a persistent browser session. It should not try to bypass portal protections or discover enforcement thresholds.
