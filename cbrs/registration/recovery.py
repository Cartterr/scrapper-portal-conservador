"""Candidate recovery for one signup identity, isolated from the live worker."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from datetime import datetime, timezone

from ..dataimpulse import build_dataimpulse_proxy_url, classify_dataimpulse_failure
from ..safety import StopReason
from .email_inbox import build_activation_address
from .identity import RegistrationIdentity, build_identity
from .orchestrator import RegistrationResult, choose_sticky_port
from .portal import PortalError
from .routes import RegistrationRouteStore, RouteRecoveryStop, production_exclusions

logger = logging.getLogger(__name__)


def register_with_route_recovery(orchestrator, *, dry_run: bool = False) -> list[RegistrationResult]:
    """One identity, finite candidates; never rotate after accepted/unknown create.

    A generic registration failure may advance only when the server explicitly
    returned the known HTTP-400 rejection. This deliberate recovery opt-in does
    not change the ordinary one-route CLI or production account recovery.
    """
    settings = orchestrator.settings
    settings.require_proxy()
    if not dry_run:
        settings.require_activation_inbox()
    # Do not reserve an identity unless production exclusions are readable.
    production_exclusions(settings.pool_state_path)
    ledger = RegistrationRouteStore(
        settings.profile_root.parent / "registration" / "routes.sqlite3",
        hourly_limit=settings.route_hourly_limit,
        daily_submit_limit=settings.route_daily_submit_limit,
    )
    record = orchestrator.rut_provider.reserve_next()
    identity_key = hashlib.sha256(record.rut.encode()).hexdigest()[:12]
    retry_record = None if dry_run else orchestrator.store.registration_retry_for_rut(record.rut)
    if retry_record:
        identity = RegistrationIdentity(orchestrator._person(record), retry_record.email,
                                        retry_record.password, retry_record.direccion, retry_record.celular)
        logger.info("Continuing the saved registration identity; credentials and form data are unchanged.")
    else:
        identity = build_identity(
            orchestrator._person(record),
            email_address=build_activation_address(settings.activation_domain or "dry-run.invalid", record.rut, orchestrator._rng),
            address_pool=settings.address_pool, celular=orchestrator._celular(), rng=orchestrator._rng,
        )
    started = orchestrator._now()
    release = True
    stored = retry_record is not None
    if stored:
        orchestrator.store.clear_failure(identity.email, "created")
    paid_used = 0
    last_reason = "No candidate passed registration preflight."
    run_id = datetime.now(timezone.utc).strftime("recovery-%Y%m%d-%H%M%S")
    try:
        for index in range(settings.route_candidates):
            try:
                production_ports, production_exits = production_exclusions(settings.pool_state_path)
                old_ports, old_exits = ledger.exclusions()
                attempt = ledger.reserve(identity_key)
            except RouteRecoveryStop as exc:
                last_reason = str(exc)
                logger.warning("%s", last_reason)
                break
            port = choose_sticky_port(settings, orchestrator.store, orchestrator._rng,
                                      excluded_ports=production_ports | old_ports)
            ledger.assign(attempt, port=port)
            proxy = build_dataimpulse_proxy_url(
                login=settings.dataimpulse_login, password=settings.dataimpulse_password,
                host=settings.dataimpulse_host, country=settings.dataimpulse_country,
                ttl_minutes=settings.dataimpulse_ttl_minutes, port=port,
                scheme=settings.dataimpulse_scheme, port_min=settings.dataimpulse_port_min,
                port_max=settings.dataimpulse_port_max,
            )
            candidate_settings = replace(settings, max_paid_solves=max(0, settings.max_paid_solves - paid_used))
            profile = settings.profile_root / f"{run_id}-candidate-{index+1:02d}"
            portal = orchestrator._portal_factory(candidate_settings, proxy, profile)
            logger.info("Registration route candidate %d/%d: sticky port %d, profile %s",
                        index + 1, settings.route_candidates, port, profile)
            stage = "preflight"
            accepted = False
            should_wait = False
            try:
                # Check egress before even loading the public form. The same
                # sticky proxy is installed in the candidate Chrome afterwards.
                portal.check_egress_country(settings.dataimpulse_country)
                egress = getattr(portal, "egress_hash", None)
                ledger.assign(attempt, port=port, egress_hash=egress)
                if not egress:
                    ledger.finish(attempt, "no_egress_identity")
                    continue
                if egress in production_exits or egress in old_exits:
                    ledger.finish(attempt, "candidate_exit_reused")
                    logger.info("Candidate skipped: exit is production-used or already attempted.")
                    continue
                stage = "launch"
                portal.open()
                stage = "bootstrap"
                portal.bootstrap()
                if dry_run:
                    portal.prepare_registration_form(identity.create_payload(""))
                    token = portal.recaptcha_token("signup")
                    logger.info("Candidate dry run passed: form verified, token length %d; no submit.", len(token))
                    ledger.finish(attempt, "dry_run_passed")
                    return [RegistrationResult(record.rut, identity.email, "dry_run")]
                stage = "create"
                if not stored:
                    orchestrator.store.insert_created(
                        email=identity.email, password=identity.password, rut=record.rut,
                        nombres=identity.person.nombres, apellido_paterno=identity.person.apellido_paterno,
                        apellido_materno=identity.person.apellido_materno, direccion=identity.direccion,
                        celular=identity.celular, proxy_port=port,
                    )
                    stored = True
                else:
                    orchestrator.store.set_proxy_port(identity.email, port)
                ledger.mark_submitted(attempt)
                orchestrator._human_pause()
                portal.create_account(identity.create_payload(""))
                accepted = True
                release = False
                orchestrator.rut_provider.mark_registered(record.rut)
                ledger.finish(attempt, "create_accepted")
                logger.info("Candidate signup accepted; retaining this exact exit for activation and verification.")
                inbox = orchestrator._inbox_factory(settings)
                return [orchestrator._finish_created_registration(portal, inbox, record, identity, started)]
            except RouteRecoveryStop as exc:
                ledger.finish(attempt, "budget_stopped")
                last_reason = str(exc)
                break
            except PortalError as exc:
                if exc.submission_unknown or accepted:
                    release = False
                    ledger.finish(attempt, "create_indeterminate", http_status=exc.http_status, response_code=exc.response_code)
                    return [RegistrationResult(record.rut, identity.email, "created", stage_failed=exc.stage,
                                               reason="Creation outcome requires reconciliation; no route retry.")]
                captcha_rejected = (stage == "create" and exc.stop_reason == StopReason.CAPTCHA_REJECTED
                                    and exc.http_status in {200,400}
                                    and exc.response_code in {"captcha-rechazado","captcha-invalid"})
                if exc.route_retry_allowed or captcha_rejected:
                    outcome = "candidate_captcha_rejected" if captcha_rejected else (
                        "candidate_no_submission" if exc.stage == "form" else "candidate_create_rejected")
                    ledger.finish(attempt, outcome,
                                  http_status=exc.http_status, response_code=exc.response_code)
                    last_reason = "No candidate succeeded; this pass ended with known, safe-to-retry failures."
                    should_wait = True
                    logger.info("Candidate rejected signup or failed before POST; trying a distinct exit after the configured delay.")
                elif stage in {"preflight", "bootstrap"} and exc.http_status not in {403, 407, 429}:
                    ledger.finish(attempt, "candidate_connectivity_failed", http_status=exc.http_status)
                    last_reason = "Candidate preflight or bootstrap failed."
                else:
                    ledger.finish(attempt, "terminal_stop", http_status=exc.http_status, response_code=exc.response_code)
                    if stored:
                        result = orchestrator._fail(record, identity, exc, consumed=not exc.retryable)
                        release = False  # _fail has resolved the RUT lifecycle.
                        return [result]
                    last_reason = f"Registration stopped at {exc.stage}; no route retry."
                    break
            except Exception as exc:
                if stage == "create" or accepted:
                    release = False
                    ledger.finish(attempt, "create_indeterminate")
                    return [RegistrationResult(record.rut, identity.email, "created", stage_failed="create",
                                               reason="Submission outcome unknown; no route retry.")]
                # Match existing recovery's transport/terminal distinction,
                # without logging exception text or proxy credentials.
                detail = str(exc)
                terminal = classify_dataimpulse_failure(None, detail) == "provider_terminal" or "407" in detail
                network = any(marker in detail.upper() for marker in
                              ("ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED", "ERR_CONNECTION_RESET", "TIMEOUT"))
                ledger.finish(attempt, "candidate_provider_terminal" if terminal else "candidate_launch_failed")
                last_reason = "Candidate provider or browser launch failed."
                if terminal or not network:
                    break
            finally:
                paid_used += getattr(portal, "_paid_solves", 0)
                portal.close()  # Only this function's temporary candidate.
            if should_wait and index + 1 < settings.route_candidates:
                orchestrator._sleep(settings.route_retry_seconds)
        if stored:
            orchestrator.store.mark_failed(identity.email, "route_recovery", last_reason)
        return [RegistrationResult(record.rut, identity.email, "failed", stage_failed="route_recovery", reason=last_reason)]
    finally:
        if release:
            orchestrator.rut_provider.release(record.rut)
