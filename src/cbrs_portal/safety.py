from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import ClassifiedResponse, ErrorCode

logger = logging.getLogger(__name__)


class SafetyStop(RuntimeError):
    """Raised when live safeguards require operator action before continuing."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LiveLockError(SafetyStop):
    """Raised when another live CBRS process owns the browser/profile lock."""


@dataclass(frozen=True)
class SafetyPolicy:
    min_request_delay_ms: int = 30000
    request_jitter_percent: int = 20
    transient_backoff_ms: tuple[int, ...] = (120000, 300000, 600000)
    lock_stale_after_seconds: int = 3600
    token_max_age_seconds: int = 90


class LiveSafetyGovernor:
    """Persistent signal-based guardrails for CBRS device/IP safety."""

    def __init__(self, store, profile_path: Path, policy: SafetyPolicy | None = None, *, owner: str):
        self.store = store
        self.profile_path = Path(profile_path)
        self.policy = policy or SafetyPolicy()
        self.owner = owner
        self.transient_failures = 0
        self._last_request_at = 0.0
        self._lock_acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    def acquire(self) -> None:
        self.ensure_ready()
        lock = self.store.acquire_live_lock(
            owner=self.owner,
            profile_path=self.profile_path,
            stale_after_seconds=self.policy.lock_stale_after_seconds,
        )
        if not lock["acquired"]:
            raise LiveLockError(
                "another live CBRS command is running; use `cbrs safety status` for details"
            )
        self._lock_acquired = True
        self.store.record_safety_event(
            event="lock_acquired",
            endpoint=None,
            status=None,
            classified_code=None,
            message="live session lock acquired",
        )

    def release(self) -> None:
        if not self._lock_acquired:
            return
        self.store.release_live_lock(owner=self.owner)
        self.store.record_safety_event(
            event="lock_released",
            endpoint=None,
            status=None,
            classified_code=None,
            message="live session lock released",
        )
        self._lock_acquired = False

    def ensure_ready(self) -> None:
        status = self.store.safety_status(profile_path=self.profile_path)
        state = status["state"]
        if state == "ok":
            return
        action = status.get("operator_action") or "run `cbrs safety status`"
        raise SafetyStop(f"live safety state is {state}; {action}")

    def before_request(self, endpoint: str) -> None:
        self.ensure_ready()
        self._throttle(endpoint, reason="base pacing")
        self.store.record_safety_event(
            event="request_start",
            endpoint=endpoint,
            status=None,
            classified_code=None,
            message="starting live portal request",
        )

    def after_response(
        self,
        endpoint: str,
        *,
        status: int,
        classified: ClassifiedResponse,
    ) -> None:
        self.store.record_safety_event(
            event="response",
            endpoint=endpoint,
            status=status,
            classified_code=str(classified.code),
            message=classified.message,
        )
        if classified.code is ErrorCode.OK:
            self.transient_failures = 0
            self.store.record_live_success(endpoint=endpoint, status=status)
            return
        if classified.code is ErrorCode.TRANSIENT:
            self.transient_failures += 1
            self._backoff(endpoint)
            return
        if classified.code is ErrorCode.AUTH:
            if endpoint == "/api/v1/auth/refresh":
                return
            self._manual_stop(
                state="auth_required",
                signal=str(classified.code),
                endpoint=endpoint,
                status=status,
                reason=classified.message,
                operator_action="run `cbrs init` or restore the logged-in browser profile",
            )
        if classified.code in {
            ErrorCode.WAF,
            ErrorCode.CAPTCHA,
            ErrorCode.RATE_LIMIT,
            ErrorCode.DAILY_LIMIT,
        }:
            self._manual_stop(
                state="manual_required",
                signal=str(classified.code),
                endpoint=endpoint,
                status=status,
                reason=classified.message,
                operator_action="stop live work, inspect the browser/session, then run `cbrs safety unlock --reason ...`",
            )

    def assert_fresh_token(self, *, token_age_seconds: float, endpoint: str) -> None:
        if token_age_seconds <= self.policy.token_max_age_seconds:
            return
        raise SafetyStop(
            f"reCAPTCHA token aged {token_age_seconds:.1f}s before {endpoint}; fresh token required"
        )

    def _manual_stop(
        self,
        *,
        state: str,
        signal: str,
        endpoint: str,
        status: int,
        reason: str,
        operator_action: str,
    ) -> None:
        self.store.set_safety_state(
            state=state,
            signal=signal,
            endpoint=endpoint,
            status=status,
            reason=reason,
            profile_path=self.profile_path,
            operator_action=operator_action,
        )
        raise SafetyStop(f"{signal} on {endpoint} ({status}); {operator_action}")

    def _throttle(self, endpoint: str, *, reason: str) -> None:
        delay = self._delay_seconds()
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < delay:
            wait = delay - elapsed
            logger.info("CBRS pacing %.1fs before %s (%s)", wait, endpoint, reason)
            self.store.record_safety_event(
                event="wait",
                endpoint=endpoint,
                status=None,
                classified_code=None,
                message=f"waiting {wait:.1f}s: {reason}",
            )
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _delay_seconds(self) -> float:
        base = max(0, self.policy.min_request_delay_ms) / 1000
        jitter_percent = max(0, self.policy.request_jitter_percent)
        if base == 0 or jitter_percent == 0:
            return base
        spread = base * jitter_percent / 100
        return max(0, base + random.uniform(-spread, spread))

    def _backoff(self, endpoint: str) -> None:
        index = min(self.transient_failures - 1, len(self.policy.transient_backoff_ms) - 1)
        delay = max(0, self.policy.transient_backoff_ms[index]) / 1000
        logger.info("CBRS transient backoff %.1fs after %s", delay, endpoint)
        self.store.record_safety_event(
            event="backoff",
            endpoint=endpoint,
            status=None,
            classified_code=str(ErrorCode.TRANSIENT),
            message=f"transient backoff {delay:.1f}s",
        )
        time.sleep(delay)
