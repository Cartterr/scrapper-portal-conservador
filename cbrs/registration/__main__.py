"""CLI for the CBRS account-registration pipeline.

Usage::

    python -m cbrs.registration register --count 1 [--dry-run]
    python -m cbrs.registration resume-pending
    python -m cbrs.registration verify --email account@domain
    python -m cbrs.registration check-inbox
    python -m cbrs.registration status
    python -m cbrs.registration export-pool
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .config import load_registration_settings
from .email_inbox import ImapActivationInbox
from .orchestrator import RegistrationOrchestrator, RegistrationResult
from .portal import RegistrationPortal, PortalError
from .rut_provider import NoRutsAvailable
from .store import AccountStore, append_to_account_pool


def _print_results(results: list[RegistrationResult]) -> None:
    for result in results:
        line = f"[{result.status.upper():>8}] {result.rut} -> {result.email}"
        if result.stage_failed:
            line += f" (failed at {result.stage_failed}: {result.reason})"
        if result.portal_user_id:
            line += f" (portal user id {result.portal_user_id})"
        print(line)


def _cmd_register(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    if args.screenshots:
        settings = replace(settings, capture_screenshots=True)
    if args.external_captcha:
        settings = replace(settings, external_captcha=True)
    orchestrator = RegistrationOrchestrator(settings)
    try:
        if args.recover_routes:
            if args.count != 1:
                print("Route recovery requires --count 1 to preserve one identity across candidates.", file=sys.stderr)
                return 2
            from .recovery import register_with_route_recovery
            from .routes import RouteRecoveryStop
            try:
                results = register_with_route_recovery(orchestrator, dry_run=args.dry_run)
            except RouteRecoveryStop as exc:
                print(str(exc), file=sys.stderr)
                return 1
        else:
            results = orchestrator.register_accounts(count=args.count, dry_run=args.dry_run)
    except NoRutsAvailable as exc:
        print(f"No RUTs available: {exc}", file=sys.stderr)
        return 2
    except PortalError as exc:
        print(f"Registration stopped at {exc.stage}: {exc}", file=sys.stderr)
        return 1
    _print_results(results)
    if len(results) < args.count:
        print(
            f"RUT pool ran dry: only {len(results)} of {args.count} accounts attempted; "
            "add RUTs (see step 2) and rerun.",
            file=sys.stderr,
        )
    if args.dry_run:
        ok = all(item.status == "dry_run" for item in results)
        print("\nDry run complete: nothing was submitted; RUTs released." if ok
              else "\nDry run failed.", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    verified = sum(1 for item in results if item.status == "verified")
    print(f"\n{verified}/{len(results)} accounts fully verified.")
    return 0 if verified == len(results) else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    orchestrator = RegistrationOrchestrator(settings)
    results = orchestrator.resume_pending()
    if not results:
        print("Nothing pending: no accounts in status created/activated.")
        return 0
    _print_results(results)
    return 0 if all(item.status == "verified" for item in results) else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    settings.require_proxy()
    store = AccountStore(settings.accounts_db_path)
    record = store.get(args.email)
    if record is None:
        print(f"Unknown account: {args.email}", file=sys.stderr)
        return 2
    from .orchestrator import choose_sticky_port
    from ..dataimpulse import build_dataimpulse_proxy_url
    import random
    from pathlib import Path
    from datetime import datetime, timezone

    port = choose_sticky_port(settings, store, random.Random())
    proxy_url = build_dataimpulse_proxy_url(
        login=settings.dataimpulse_login,
        password=settings.dataimpulse_password,
        host=settings.dataimpulse_host,
        country=settings.dataimpulse_country,
        ttl_minutes=settings.dataimpulse_ttl_minutes,
        port=port,
        scheme=settings.dataimpulse_scheme,
        port_min=settings.dataimpulse_port_min,
        port_max=settings.dataimpulse_port_max,
    )
    profile_dir = settings.profile_root / (
        "verify-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    with RegistrationPortal(settings, proxy_url=proxy_url, profile_dir=profile_dir) as portal:
        try:
            portal.check_egress_country(settings.dataimpulse_country)
            portal.bootstrap()
            jwt = portal.login(record.email, record.password)
            me = portal.fetch_me(jwt)
        except PortalError as exc:
            print(f"Verification failed at {exc.stage}: {exc}", file=sys.stderr)
            return 1
    user_id = me.get("idUsuario") if isinstance(me, dict) else None
    store.mark_verified(record.email, portal_user_id=user_id)
    print(f"Login OK for {record.email} (portal user id {user_id}).")
    return 0


def _cmd_refill(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    from .rut_refill import DEFAULT_SOURCE, RefillSource, RutRefiller

    source = DEFAULT_SOURCE
    if args.source:
        source = RefillSource(name="custom", search_url=args.source)

    proxy_url = None
    if not args.direct:
        settings.require_proxy()
        import random

        from ..dataimpulse import build_dataimpulse_proxy_url
        from .orchestrator import choose_sticky_port

        store = AccountStore(settings.accounts_db_path)
        port = choose_sticky_port(settings, store, random.Random())
        proxy_url = build_dataimpulse_proxy_url(
            login=settings.dataimpulse_login,
            password=settings.dataimpulse_password,
            host=settings.dataimpulse_host,
            country=settings.dataimpulse_country,
            ttl_minutes=settings.dataimpulse_ttl_minutes,
            port=port,
            scheme=settings.dataimpulse_scheme,
            port_min=settings.dataimpulse_port_min,
            port_max=settings.dataimpulse_port_max,
        )
        print(f"Refilling through fresh DataImpulse sticky port {port}...")
    else:
        print("Refilling through the direct local connection...")

    terms = [term.strip() for term in args.terms.split(",") if term.strip()] if args.terms else None
    with RutRefiller(
        settings.rut_db_path,
        proxy_url=proxy_url,
        headless=False,
        source=source,
        request_delay_seconds=settings.request_delay_seconds,
    ) as refiller:
        report = refiller.refill(args.count, terms=terms)
    print(
        f"Inserted {report.inserted} new RUTs "
        f"({report.duplicates} duplicates, {report.invalid} invalid) "
        f"using terms {report.terms_used or '(none)'}."
    )
    for error in report.errors:
        print(f"  error: {error}", file=sys.stderr)
    return 0 if report.inserted > 0 or not report.errors else 1


def _cmd_check_inbox(_args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    try:
        settings.require_activation_inbox()
    except RuntimeError as exc:
        print(f"Activation inbox not configured: {exc}", file=sys.stderr)
        return 2
    candidates = (
        (settings.imap_mailbox, "INBOX")
        if settings.imap_mailbox
        else ("[Gmail]/All Mail", "INBOX")
    )
    inbox = ImapActivationInbox(
        settings.imap_host,
        settings.imap_port,
        settings.imap_user,
        settings.imap_password,
        mailbox_candidates=candidates,
        use_ssl=settings.imap_ssl,
        starttls=settings.imap_starttls,
        timeout_seconds=30.0,
    )
    transport = "SSL" if settings.imap_ssl else ("STARTTLS" if settings.imap_starttls else "plain")
    print(
        f"Connecting to {settings.imap_host}:{settings.imap_port} "
        f"({transport}) as {settings.imap_user}..."
    )
    try:
        folders = inbox.list_folders()
    except Exception as exc:
        print(f"IMAP connection failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("IMAP OK. Folders:")
    for folder in folders:
        print(f"  {folder}")
    present = [name for name in candidates if name in folders or name == "INBOX"]
    print("Read folder: " + (present[0] if present else "INBOX (fallback)"))
    print(
        "Note: the activation address must be on "
        f"{settings.activation_domain} with a catch-all forwarding to this mailbox."
    )
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    store = AccountStore(settings.accounts_db_path)
    print("RUT pool (rut_data.db):")
    try:
        from .rut_provider import RutProvider

        for status, count in sorted(RutProvider(settings.rut_db_path).counts().items()):
            print(f"  {status}: {count}")
    except RuntimeError as exc:
        print(f"  unavailable: {exc}")
    print("Registered accounts (registered_accounts.db):")
    counts = store.counts()
    if not counts:
        print("  (none)")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    inbox_missing = not (
        settings.activation_domain and settings.imap_user and settings.imap_password
    )
    print(
        "Activation inbox: "
        + ("NOT configured (set CBRS_ACTIVATION_* in .env)" if inbox_missing else "configured")
    )
    return 0


def _cmd_export_pool(args: argparse.Namespace) -> int:
    settings = load_registration_settings()
    store = AccountStore(settings.accounts_db_path)
    entries = store.export_pool_entries()
    if not entries:
        print("No verified accounts to export.")
        return 0
    out_dir = settings.profile_root.parent / "registration"
    pool_path = Path(args.pool) if args.pool else out_dir / "worker-pool.patch.json"
    env_path = Path(args.env_file) if args.env_file else out_dir / "credentials.env"
    added = append_to_account_pool(pool_path, entries)

    lines = store.export_env_assignments()
    existing = (
        env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    )
    known = {line.split("=", 1)[0] for line in existing if "=" in line}
    merged = existing + [line for line in lines if line.split("=", 1)[0] not in known]
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8")

    print(f"Exported {added} pool entries to {pool_path} "
          f"({len(entries) - added} already present).")
    print(f"Credentials for {len(entries)} verified accounts written to {env_path} "
          "(secret file; keep it out of git).")
    print(
        "Next: 1) add the CBRS_REGISTRO_* lines from that file to .env, "
        "2) merge the patch accounts into the pool file the worker reads "
        f"({settings.profile_root.parent / 'runtime' / 'account-pool.json'}), "
        '3) set "enabled": true for the accounts the worker should use.'
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cbrs.registration",
        description="CBRS portal account registration pipeline",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="register new accounts end to end")
    register.add_argument("--count", type=int, default=1)
    register.add_argument(
        "--recover-routes", action="store_true",
        help="test bounded, distinct DataImpulse candidates for one identity using the configured recovery allowances",
    )
    register.add_argument(
        "--screenshots", action="store_true",
        help="save masked screenshots at registration stages under the run's profile",
    )
    register.add_argument(
        "--external-captcha", action="store_true",
        help="allow bounded CapSolver then 2Captcha fallback after explicit CAPTCHA rejection "
        "or browser token failure (at most four paid tasks per run)",
    )
    register.add_argument(
        "--dry-run",
        action="store_true",
        help="reserve RUT, open browser, fill and verify the visible form, "
        "prove egress+bootstrap+signup captcha, then release without submitting",
    )
    register.set_defaults(func=_cmd_register)

    resume = sub.add_parser(
        "resume-pending", help="finish accounts stuck at created/activated"
    )
    resume.set_defaults(func=_cmd_resume)

    refill = sub.add_parser("refill", help="scrape new RUTs into rut_data.db")
    refill.add_argument("--count", type=int, default=50)
    refill.add_argument(
        "--terms",
        help="comma-separated search terms (default: simple numeric sequence)",
    )
    refill.add_argument("--source", help="alternate search page URL")
    refill.add_argument(
        "--direct", action="store_true", help="no proxy (sites may 403)"
    )
    refill.set_defaults(func=_cmd_refill)

    verify = sub.add_parser("verify", help="login-test one stored account")
    verify.add_argument("--email", required=True)
    verify.set_defaults(func=_cmd_verify)

    status = sub.add_parser("status", help="show RUT pool and account counts")
    status.set_defaults(func=_cmd_status)

    check_inbox = sub.add_parser(
        "check-inbox",
        help="verify the activation mailbox over IMAP (lists folders, reads no mail)",
    )
    check_inbox.set_defaults(func=_cmd_check_inbox)

    export = sub.add_parser(
        "export-pool",
        help="write worker-pool entries + env credentials for verified accounts",
    )
    export.add_argument(
        "--pool",
        help="pool JSON to merge into (default: .cbrs/registration/worker-pool.patch.json)",
    )
    export.add_argument(
        "--env-file",
        help="env file for the generated credentials (default: .cbrs/registration/credentials.env)",
    )
    export.set_defaults(func=_cmd_export_pool)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
