from __future__ import annotations

import json
import random
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import SETTINGS, Settings
from .safety import redact, redact_text
from .validation import ValidationRunResult, run_controlled_validation

DEFAULT_SOAK_CONFIG = ".cbrs/soak-config.json"
DEFAULT_INTERVAL_MIN_MINUTES = 2.0
DEFAULT_INTERVAL_MAX_MINUTES = 4.0
DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8765
DEFAULT_TARGET_LABEL = "default_safe_query"
DEFAULT_TARGET_QUERY = "BANCO DE CHILE"


@dataclass(frozen=True)
class SoakTarget:
    label: str
    kind: str
    query: str | None = None
    foja: int | None = None
    numero: int | None = None
    ano: int | None = None


@dataclass(frozen=True)
class SoakConfig:
    interval_min_minutes: float
    interval_max_minutes: float
    dashboard_host: str
    dashboard_port: int
    targets: tuple[SoakTarget, ...]


@dataclass(frozen=True)
class SoakRunResult:
    exit_code: int
    run_id: str
    status: str
    dashboard_url: str | None = None


class SoakStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    next_cycle_at TEXT,
                    blocked_reason TEXT,
                    dry_run INTEGER NOT NULL,
                    dashboard_url TEXT,
                    interval_min_minutes REAL NOT NULL,
                    interval_max_minutes REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cycles (
                    cycle_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    target_label TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_seconds REAL,
                    result_count INTEGER,
                    download_count INTEGER NOT NULL DEFAULT 0,
                    validation_report_path TEXT,
                    preflight_report_path TEXT,
                    artifact_path TEXT,
                    artifact_size_bytes INTEGER,
                    safety_stop TEXT,
                    error TEXT,
                    next_cycle_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    cycle_id TEXT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT
                );

                CREATE TABLE IF NOT EXISTS control (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create_run(
        self,
        *,
        run_id: str,
        dry_run: bool,
        config: SoakConfig,
        dashboard_url: str | None,
    ) -> None:
        now = utc_now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO runs (
                    run_id, started_at, status, heartbeat_at, dry_run,
                    dashboard_url, interval_min_minutes, interval_max_minutes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now,
                    "running",
                    now,
                    1 if dry_run else 0,
                    dashboard_url,
                    config.interval_min_minutes,
                    config.interval_max_minutes,
                ),
            )

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        next_cycle_at: str | None = None,
        blocked_reason: str | None = None,
        finished: bool = False,
    ) -> None:
        fields = ["heartbeat_at = ?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if next_cycle_at is not None:
            fields.append("next_cycle_at = ?")
            values.append(next_cycle_at)
        if blocked_reason is not None:
            fields.append("blocked_reason = ?")
            values.append(redact_text(blocked_reason))
        if finished:
            fields.append("finished_at = ?")
            values.append(utc_now())
        values.append(run_id)
        with self._connect() as db:
            db.execute(f"UPDATE runs SET {', '.join(fields)} WHERE run_id = ?", values)

    def add_event(
        self,
        run_id: str,
        *,
        message: str,
        level: str = "info",
        cycle_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO events (run_id, cycle_id, created_at, level, message, data_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    cycle_id,
                    utc_now(),
                    level,
                    message,
                    json.dumps(redact(data or {}), ensure_ascii=False),
                ),
            )

    def start_cycle(
        self,
        *,
        cycle_id: str,
        run_id: str,
        sequence: int,
        target: SoakTarget,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO cycles (
                    cycle_id, run_id, sequence, target_label, target_kind,
                    action, status, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    run_id,
                    sequence,
                    target.label,
                    target.kind,
                    "full_flow_download_first",
                    "running",
                    utc_now(),
                ),
            )

    def finish_cycle(
        self,
        cycle_id: str,
        *,
        status: str,
        started_at_monotonic: float,
        result_count: int | None = None,
        download_count: int = 0,
        validation_report_path: Path | None = None,
        preflight_report_path: Path | None = None,
        artifact_path: Path | None = None,
        artifact_size_bytes: int | None = None,
        safety_stop: str | None = None,
        error: str | None = None,
        next_cycle_at: str | None = None,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE cycles
                SET status = ?,
                    finished_at = ?,
                    duration_seconds = ?,
                    result_count = ?,
                    download_count = ?,
                    validation_report_path = ?,
                    preflight_report_path = ?,
                    artifact_path = ?,
                    artifact_size_bytes = ?,
                    safety_stop = ?,
                    error = ?,
                    next_cycle_at = ?
                WHERE cycle_id = ?
                """,
                (
                    status,
                    utc_now(),
                    round(time.monotonic() - started_at_monotonic, 3),
                    result_count,
                    download_count,
                    str(validation_report_path) if validation_report_path else None,
                    str(preflight_report_path) if preflight_report_path else None,
                    str(artifact_path) if artifact_path else None,
                    artifact_size_bytes,
                    safety_stop,
                    redact_text(error) if error else None,
                    next_cycle_at,
                    cycle_id,
                ),
            )

    def latest_run(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def recent_cycles(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE run_id = ?" if run_id else ""
        params: tuple[Any, ...] = (run_id, limit) if run_id else (limit,)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT * FROM cycles
                {where}
                ORDER BY started_at DESC, sequence DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_events(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "WHERE run_id = ?" if run_id else ""
        params: tuple[Any, ...] = (run_id, limit) if run_id else (limit,)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT * FROM events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def artifacts(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where = "AND run_id = ?" if run_id else ""
        params: tuple[Any, ...] = (run_id, limit) if run_id else (limit,)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT cycle_id, run_id, sequence, target_label, artifact_path,
                       artifact_size_bytes, finished_at
                FROM cycles
                WHERE artifact_path IS NOT NULL
                {where}
                ORDER BY finished_at DESC, sequence DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def export_snapshot(self) -> dict[str, Any]:
        return redact({
            "schema": "cbrs-soak-export-v1",
            "exported_at": utc_now(),
            "latest_run": self.latest_run(),
            "cycles": self.recent_cycles(limit=1000),
            "events": self.recent_events(limit=1000),
            "artifacts": self.artifacts(limit=1000),
        })

    def request_stop(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO control (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at = excluded.updated_at
                """,
                ("stop_requested_at", utc_now(), utc_now()),
            )

    def clear_stop_request(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM control WHERE key = ?", ("stop_requested_at",))

    def stop_requested(self) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM control WHERE key = ?",
                ("stop_requested_at",),
            ).fetchone()
        return row is not None

    def control_state(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT key, value, updated_at FROM control").fetchall()
        return {str(row["key"]): {"value": row["value"], "updated_at": row["updated_at"]} for row in rows}

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db


def load_soak_config(
    settings: Settings = SETTINGS,
    *,
    path: Path | None = None,
) -> SoakConfig:
    path = path or settings.profile_dir.parent / "soak-config.json"
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = {}
    targets = tuple(_parse_targets(raw.get("targets")))
    config = SoakConfig(
        interval_min_minutes=float(
            raw.get("interval_min_minutes", DEFAULT_INTERVAL_MIN_MINUTES)
        ),
        interval_max_minutes=float(
            raw.get("interval_max_minutes", DEFAULT_INTERVAL_MAX_MINUTES)
        ),
        dashboard_host=str(raw.get("dashboard_host", DEFAULT_DASHBOARD_HOST)),
        dashboard_port=int(raw.get("dashboard_port", DEFAULT_DASHBOARD_PORT)),
        targets=targets or (
            SoakTarget(
                label=DEFAULT_TARGET_LABEL,
                kind="text",
                query=DEFAULT_TARGET_QUERY,
            ),
        ),
    )
    if config.interval_min_minutes < 0 or config.interval_max_minutes < 0:
        raise ValueError("soak intervals must be zero or greater")
    if config.interval_max_minutes < config.interval_min_minutes:
        raise ValueError("interval_max_minutes must be >= interval_min_minutes")
    return config


def choose_target(config: SoakConfig, rng: random.Random | None = None) -> SoakTarget:
    rng = rng or random.Random()
    return rng.choice(config.targets)


def next_interval_seconds(
    config: SoakConfig,
    rng: random.Random | None = None,
) -> float:
    rng = rng or random.Random()
    return rng.uniform(
        config.interval_min_minutes * 60,
        config.interval_max_minutes * 60,
    )


def default_soak_store(settings: Settings = SETTINGS) -> SoakStore:
    return SoakStore(settings.profile_dir.parent / "soak" / "soak.sqlite3")


def run_soak(
    *,
    settings: Settings = SETTINGS,
    config: SoakConfig | None = None,
    store: SoakStore | None = None,
    dry_run: bool = False,
    max_cycles: int | None = None,
    dashboard: bool = False,
    headless: bool | None = None,
    rng: random.Random | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    validation_runner: Callable[..., ValidationRunResult] = run_controlled_validation,
    on_dashboard_start: Callable[[str], None] | None = None,
) -> SoakRunResult:
    from .soak_dashboard import start_dashboard

    config = config or load_soak_config(settings)
    store = store or default_soak_store(settings)
    rng = rng or random.Random()
    run_id = (
        f"soak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
        f"{secrets.token_hex(3)}"
    )
    dashboard_server = None
    dashboard_url = None
    if dashboard:
        dashboard_server = start_dashboard(
            store,
            settings=settings,
            host=config.dashboard_host,
            port=config.dashboard_port,
        )
        dashboard_url = dashboard_server.url
        if on_dashboard_start:
            on_dashboard_start(dashboard_url)

    store.clear_stop_request()
    store.create_run(
        run_id=run_id,
        dry_run=dry_run,
        config=config,
        dashboard_url=dashboard_url,
    )
    store.add_event(
        run_id,
        message="soak run started",
        data={"dry_run": dry_run, "dashboard_url": dashboard_url},
    )

    sequence = 0
    final_status = "completed"
    exit_code = 0
    try:
        while max_cycles is None or sequence < max_cycles:
            if store.stop_requested():
                final_status = "stopped"
                store.add_event(run_id, message="soak run stop requested")
                break
            sequence += 1
            target = choose_target(config, rng)
            cycle_id = f"{run_id}-cycle-{sequence:04d}"
            output_dir = settings.output_dir / "soak" / run_id / f"cycle-{sequence:04d}"
            started = time.monotonic()
            store.update_run(run_id, status="running", next_cycle_at="")
            store.start_cycle(
                cycle_id=cycle_id,
                run_id=run_id,
                sequence=sequence,
                target=target,
            )
            store.add_event(
                run_id,
                cycle_id=cycle_id,
                message="cycle started",
                data={"target_label": target.label, "target_kind": target.kind},
            )

            with _heartbeat_while_cycle_runs(store, run_id):
                result = (
                    _dry_run_validation(settings, target, output_dir)
                    if dry_run
                    else validation_runner(
                        settings=settings,
                        search_kind=target.kind,
                        query=target.query,
                        foja=target.foja,
                        numero=target.numero,
                        ano=target.ano,
                        download_first=True,
                        output_dir=output_dir,
                        keep_images=False,
                        headless=headless,
                    )
                )
            cycle_status = _cycle_status(result)
            store.finish_cycle(
                cycle_id,
                status=cycle_status,
                started_at_monotonic=started,
                result_count=result.result_count,
                download_count=1 if result.pdf_path else 0,
                validation_report_path=result.report_path,
                preflight_report_path=result.preflight_report_path,
                artifact_path=result.pdf_path,
                artifact_size_bytes=result.pdf_size_bytes,
                safety_stop=result.safety_stop,
                error=result.error,
            )
            store.add_event(
                run_id,
                cycle_id=cycle_id,
                level="error" if result.exit_code else "info",
                message=f"cycle {cycle_status}",
                data={
                    "status": result.status,
                    "result_count": result.result_count,
                    "safety_stop": result.safety_stop,
                    "error": result.error,
                },
            )

            if result.exit_code == 2:
                final_status = "blocked"
                exit_code = 2
                store.update_run(
                    run_id,
                    status="blocked",
                    blocked_reason=result.safety_stop or result.error or "safety_stop",
                )
                break

            if store.stop_requested():
                final_status = "stopped"
                store.add_event(run_id, message="soak run stop requested")
                break

            if max_cycles is not None and sequence >= max_cycles:
                break

            interval = 0.0 if dry_run else next_interval_seconds(config, rng)
            next_cycle_at = utc_from_epoch(time.time() + interval)
            store.finish_cycle(
                cycle_id,
                status=cycle_status,
                started_at_monotonic=started,
                result_count=result.result_count,
                download_count=1 if result.pdf_path else 0,
                validation_report_path=result.report_path,
                preflight_report_path=result.preflight_report_path,
                artifact_path=result.pdf_path,
                artifact_size_bytes=result.pdf_size_bytes,
                safety_stop=result.safety_stop,
                error=result.error,
                next_cycle_at=next_cycle_at,
            )
            store.update_run(run_id, status="waiting", next_cycle_at=next_cycle_at)
            if _sleep_with_heartbeat(store, run_id, interval, sleep_fn=sleep_fn):
                final_status = "stopped"
                store.add_event(run_id, message="soak run stop requested")
                break
    except KeyboardInterrupt:
        final_status = "stopped"
        exit_code = 0
        store.add_event(run_id, message="soak run stopped by operator")
    finally:
        if final_status != "blocked":
            store.update_run(run_id, status=final_status, finished=True)
        if dashboard_server and (final_status == "blocked" and max_cycles is None):
            _hold_dashboard(store, run_id, sleep_fn=sleep_fn)
            dashboard_server.stop()
        elif dashboard_server:
            dashboard_server.stop()

    return SoakRunResult(
        exit_code=exit_code,
        run_id=run_id,
        status=final_status,
        dashboard_url=dashboard_url,
    )


def dashboard_status(store: SoakStore) -> dict[str, Any]:
    run = store.latest_run()
    if not run:
        return {
            "schema": "cbrs-soak-status-v1",
            "status": "not_started",
            "run": None,
            "stats": {},
            "cycles": [],
            "events": [],
            "artifacts": [],
        }
    run_id = str(run["run_id"])
    cycles = store.recent_cycles(run_id=run_id, limit=100)
    events = store.recent_events(run_id=run_id, limit=100)
    artifacts = store.artifacts(run_id=run_id, limit=100)

    status = str(run["status"])
    heartbeat_age = seconds_since(str(run["heartbeat_at"]))
    if status in {"running", "waiting"} and heartbeat_age > 120:
        status = "stale"
    passed = sum(1 for cycle in cycles if cycle["status"] == "passed")
    failed = sum(1 for cycle in cycles if cycle["status"] == "failed")
    blocked = sum(1 for cycle in cycles if cycle["status"] == "blocked")
    total = len(cycles)
    completed_cycles = [
        cycle for cycle in cycles if cycle["status"] in {"passed", "failed", "blocked"}
    ]
    completed_total = len(completed_cycles)
    consecutive_successes = 0
    for cycle in completed_cycles:
        if cycle["status"] == "passed":
            consecutive_successes += 1
        else:
            break

    return {
        "schema": "cbrs-soak-status-v1",
        "status": status,
        "run": dict(run),
        "alert": _active_alert(status=status, run=run, cycles=cycles),
        "stats": {
            "total_cycles": total,
            "passed_cycles": passed,
            "failed_cycles": failed,
            "safety_stops": blocked,
            "success_rate": round(passed / completed_total, 4) if completed_total else None,
            "consecutive_successes": consecutive_successes,
            "downloads": sum(int(cycle["download_count"] or 0) for cycle in cycles),
            "heartbeat_age_seconds": heartbeat_age,
            "uptime_seconds": seconds_between(str(run["started_at"]), utc_now()),
        },
        "cycles": cycles,
        "events": events,
        "artifacts": artifacts,
        "control": store.control_state(),
    }


def _active_alert(
    *,
    status: str,
    run: dict[str, Any],
    cycles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if status != "blocked":
        return None

    blocked_cycle = next(
        (cycle for cycle in cycles if cycle.get("status") == "blocked"),
        None,
    )
    reason = (
        (blocked_cycle or {}).get("safety_stop")
        or run.get("blocked_reason")
        or (blocked_cycle or {}).get("error")
        or "safety_stop"
    )
    reason_text = str(reason)
    title = "Parada de seguridad del portal"
    summary = "La prueba continua se pausó de inmediato. No habrá más acciones en el portal hasta revisión y reinicio manual."
    if reason_text == "captcha_rejected":
        title = "Desafío CAPTCHA detectado"
        summary = "CBRS devolvió una señal relacionada con CAPTCHA. La prueba indefinida queda pausada y no seguirá enviando tráfico al portal."
    elif reason_text in {"waf_challenge", "unexpected_html"}:
        title = "Desafío del portal detectado"
        summary = "CBRS o su WAF devolvió una página de desafío. La prueba indefinida se pausó antes de cualquier otra acción."
    elif reason_text == "rate_limit":
        title = "Límite de uso detectado"
        summary = "CBRS devolvió una señal de límite de uso. La prueba indefinida queda pausada y no reintentará automáticamente."

    return redact(
        {
            "active": True,
            "severity": "critical",
            "title": title,
            "message": summary,
            "reason": reason_text,
            "cycle_sequence": (blocked_cycle or {}).get("sequence"),
            "cycle_id": (blocked_cycle or {}).get("cycle_id"),
        }
    )


def _parse_targets(raw_targets: Any) -> Iterable[SoakTarget]:
    if not raw_targets:
        return []
    targets = []
    for raw in raw_targets:
        label = str(raw.get("label") or "").strip()
        if not label:
            raise ValueError("each soak target requires a label")
        if raw.get("query"):
            targets.append(
                SoakTarget(label=label, kind="text", query=str(raw["query"]))
            )
        else:
            targets.append(
                SoakTarget(
                    label=label,
                    kind="fna",
                    foja=int(raw["foja"]),
                    numero=int(raw["numero"]),
                    ano=int(raw["ano"]),
                )
            )
    return targets


def _dry_run_validation(
    settings: Settings,
    target: SoakTarget,
    output_dir: Path,
) -> ValidationRunResult:
    from .validation import finish_validation_report, new_validation_report, write_validation_report

    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "dry-run-placeholder.pdf"
    pdf_path.write_bytes(_dry_run_pdf_bytes())
    report = new_validation_report(
        settings,
        search_kind=target.kind,
        download_first=True,
        preflight_metadata={
            "expected_egress_country": settings.expected_egress_country,
            "egress_mode": settings.egress_mode or None,
            "egress_country": "DRY_RUN",
            "egress_hash": "dry-run",
            "profile_hash": "dry-run",
            "preflight_status": "dry_run",
        },
        headless=settings.headless,
    )
    report["result_count"] = 1
    report["pdf_created"] = True
    report["pdf_path"] = str(pdf_path)
    report["pdf_size_bytes"] = pdf_path.stat().st_size
    finish_validation_report(report, status="passed")
    report_path = write_validation_report(report, settings)
    return ValidationRunResult(
        exit_code=0,
        status="passed",
        report=report,
        report_path=report_path,
        preflight_report_path=None,
        result_count=1,
        pdf_path=pdf_path,
        pdf_size_bytes=pdf_path.stat().st_size,
    )


def _cycle_status(result: ValidationRunResult) -> str:
    if result.exit_code == 0:
        return "passed"
    if result.exit_code == 2:
        return "blocked"
    return "failed"


@contextmanager
def _heartbeat_while_cycle_runs(
    store: SoakStore,
    run_id: str,
    *,
    interval_seconds: float = 30.0,
):
    stop_event = threading.Event()

    def heartbeat() -> None:
        while not stop_event.wait(interval_seconds):
            store.update_run(run_id, status="running", next_cycle_at="")

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=2.0)


def _sleep_with_heartbeat(
    store: SoakStore,
    run_id: str,
    seconds: float,
    *,
    sleep_fn: Callable[[float], None],
) -> bool:
    deadline = time.time() + seconds
    while True:
        if store.stop_requested():
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        store.update_run(run_id)
        sleep_fn(min(30.0, remaining))


def _hold_dashboard(
    store: SoakStore,
    run_id: str,
    *,
    sleep_fn: Callable[[float], None],
) -> None:
    try:
        while True:
            store.update_run(run_id, status="blocked")
            sleep_fn(30.0)
    except KeyboardInterrupt:
        store.update_run(run_id, status="stopped", finished=True)


def _dry_run_pdf_bytes() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >> endobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF\n"
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat()


def seconds_since(value: str) -> float:
    return seconds_between(value, utc_now())


def seconds_between(start: str, end: str) -> float:
    return max(0.0, (_parse_utc(end) - _parse_utc(start)).total_seconds())


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
