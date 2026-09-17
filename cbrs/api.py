"""Public local-service client. Importing this module performs no I/O."""
from __future__ import annotations

import csv
import hashlib
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


class ServiceUnavailable(RuntimeError):
    """The worker is unavailable; start it with cbrs service start worker."""


class InvalidInscription(ValueError):
    """Invalid positive integer inscription or malformed CSV structure."""


class QuotaExhausted(RuntimeError):
    """Raised by Result.raise_for_status for pending_quota results."""


class DownloadFailed(RuntimeError):
    """Raised for local output errors or by Result.raise_for_status."""


@dataclass
class Result:
    """One inscription outcome; pending results retain their durable job_id."""

    fojas: Any
    numero: Any
    ano: Any
    status: str = "pending"
    pdf_path: Path | None = None
    error: str | None = None
    account: str | None = None
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    job_id: str | None = None
    resume_at: str | None = None
    source: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe public representation."""
        data = asdict(self)
        data.pop("source")
        data["pdf_path"] = str(self.pdf_path) if self.pdf_path else None
        return data

    def raise_for_status(self) -> Result:
        """Optionally turn unsuccessful outcomes into typed exceptions."""
        if self.status == "pending_quota":
            raise QuotaExhausted(self.error or "Cuota agotada; el trabajo sigue pendiente.")
        if self.status != "done":
            raise DownloadFailed(self.error or self.status)
        return self


def _positive(value: Any) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value).strip()):
        raise InvalidInscription("fojas, numero y ano deben ser enteros positivos")
    number = int(value)
    if number <= 0:
        raise InvalidInscription("fojas, numero y ano deben ser enteros positivos")
    return number


def _timeout(value: float) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise InvalidInscription("timeout debe ser un número finito mayor o igual a cero") from exc
    if not math.isfinite(number) or number < 0:
        raise InvalidInscription("timeout debe ser un número finito mayor o igual a cero")
    return number


ALIASES = ({"foja", "fojas"}, {"numero", "número", "num"}, {"ano", "año", "anio", "year"})
REPORT_FIELDS = ("status", "pdf_path", "error", "account", "attempts", "job_id", "finished_at")


def read_csv(path: str | Path) -> tuple[list[str], list[tuple[dict[str, Any], Any, str | None]]]:
    """Parse structure strictly, retaining invalid rows for the result report."""
    rows = []
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            fields = reader.fieldnames or []
            normalized = [name.strip().lower() for name in fields]
            keys = []
            for synonyms in ALIASES:
                matches = [fields[i] for i, name in enumerate(normalized) if name in synonyms]
                if len(matches) != 1:
                    raise InvalidInscription("línea 1: se requiere una columna de fojas, numero y ano, sin ambigüedades")
                keys.append(matches[0])
            if len(set(normalized)) != len(fields) or any(name in REPORT_FIELDS for name in normalized):
                raise InvalidInscription("línea 1: encabezados duplicados o reservados para el reporte")
            for raw in reader:
                line = reader.line_num
                values = tuple(raw.get(key) for key in keys)
                error = None
                try:
                    if None in raw or any(value is None for value in raw.values()):
                        raise InvalidInscription("cantidad de columnas incorrecta")
                    values = tuple(_positive(value) for value in values)
                except InvalidInscription as exc:
                    error = f"línea {line}: {exc}"
                rows.append(({key: raw.get(key, "") for key in fields}, values, error))
    except (OSError, UnicodeError, csv.Error) as exc:
        line = reader.line_num if 'reader' in locals() else 1
        raise InvalidInscription(f"CSV inválido, línea {line}: {type(exc).__name__}") from exc
    return fields, rows


class _LocalBackend:
    def __init__(self) -> None:
        from .config import load_settings
        from .jobs import default_job_store
        self.settings = load_settings()
        from dotenv import dotenv_values
        from .paths import REPO_ROOT
        configured_output = os.environ.get("CBRS_OUTPUT_DIR") or dotenv_values(REPO_ROOT / ".env").get("CBRS_OUTPUT_DIR")
        self.export_dir = (Path(configured_output).expanduser() if configured_output else REPO_ROOT / "outputs/pdf")
        if not self.export_dir.is_absolute():
            self.export_dir = REPO_ROOT / self.export_dir
        path = self.settings.profile_dir.parent / "pool" / "pool.sqlite3"
        self.store = default_job_store(self.settings) if path.is_file() else None

    def check(self) -> None:
        if self.store is None or not self.store.active_lease():
            raise ServiceUnavailable(
                "Servicio no disponible. Con systemd: cbrs service start worker. "
                "Sin systemd: cbrs jobs worker (en otra terminal)."
            )

    def status(self) -> dict[str, Any]:
        if self.store is None:
            return {"service": False, "accounts": [], "jobs": {"counts": {}}}
        from .account_pool import AccountPoolStore, load_account_pool_config
        from .form_search import account_window, quota_hold
        pool = load_account_pool_config(self.settings)
        pool_store = AccountPoolStore(self.store.path)
        run = pool_store.latest_run(dry_run=False)
        states = ({row["account_id"]: row for row in pool_store.accounts(run["run_id"])}
                  if run else {})
        accounts = []
        for account in pool.accounts:
            if not account.enabled:
                continue
            window = account_window(self.store.path, account.account_id)
            hold = quota_hold(self.store.path, account.account_id)
            check = self.store.account_check(account.account_id) or {}
            remaining = max(0, pool.quota_for(account) - window["used"] - window["reserved"])
            blocked = bool(hold and hold["blocked"])
            state = states.get(account.account_id, {})
            reason = state.get("paused_reason") or check.get("browser_last_auth_error")
            unavailable = state.get("status") in {"paused", "captcha_pending", "captcha_solving"}
            status = ("held" if blocked else
                      "disabled" if unavailable and reason == "credentials_invalid" else
                      state["status"] if unavailable else
                      "egress_preflight_failed" if check.get("proxy_status") == "failed" else
                      "estimated_quota_reached" if not remaining and pool.enforce_estimated_quota else
                      "available" if check.get("browser_authenticated") else "login_pending")
            accounts.append({
                "account": account.account_id, "remaining_estimated": remaining,
                "status": status,
                "estimate_enforced": pool.enforce_estimated_quota,
                "portal_quota": blocked,
                "resume_at": hold.get("next_check_at") if blocked else state.get("resume_at"),
                "proxy": (self.store.dataimpulse_route(account.account_id) or {}).get("active_port", account.dataimpulse_port),
                "error": reason or ("egress_preflight_failed" if check.get("proxy_status") == "failed" else None),
                "http_status": check.get("browser_last_auth_http_status"),
            })
        return {"service": bool(self.store.active_lease()), "accounts": accounts, "jobs": self.store.summary()}

    def submit(self, values: tuple[int, int, int], force: bool) -> dict[str, Any]:
        from .jobs import stable_json
        request = dict(zip(("foja", "numero", "year"), values))
        # Reuse legacy jobs too; the durable key closes concurrent-submit races.
        if not force:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT job_id FROM jobs WHERE kind='fna' AND input_json=? AND source='production' "
                    "AND status != 'cancelled' ORDER BY CASE WHEN status='completed' THEN 0 ELSE 1 END, created_at LIMIT 1",
                    (stable_json(request),),
                ).fetchone()
            if row:
                job = self.store.get_job(row["job_id"])
                if job["status"] == "completed" and job.get("result_count") and self.pdf(job) is None:
                    self._recover_documents(job)
                    job = self.store.get_job(row["job_id"])
                elif job["status"] in {"failed", "partial"} and job.get("error_code") in {
                    "document_retrieval_deferred", "document_recovery_exhausted", "download_failed",
                }:
                    self._recover_documents(job)
                    job = self.store.get_job(row["job_id"])
                return job
        key = None if force else "get:" + ":".join(map(str, values))
        job, _ = self.store.create_job(kind="fna", input_data=request, idempotency_key=key)
        return job

    def _recover_documents(self, job: dict[str, Any]) -> None:
        """Requeue document work with its original search receipt and quota intact."""
        from .jobs import utc_now, validate_pdf
        broken = []
        for item in self.store.items(job["job_id"], public=False):
            try:
                if item["status"] != "completed":
                    raise ValueError("incomplete")
                validate_pdf(Path(item["output_path"]), expected_pages=int(item["expected_pages"]))
            except (TypeError, OSError, RuntimeError, ValueError):
                broken.append(item["item_id"])
        if not broken:
            return
        now = utc_now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE jobs SET status='queued', finished_at=NULL, next_run_at=NULL,
                updated_at=?, error_code='document_retrieval_deferred',
                error_message='Document recovery queued; search receipt retained'
                WHERE job_id=? AND status IN ('completed','partial','failed')
                AND NOT EXISTS (SELECT 1 FROM leases WHERE lease_name=? AND expires_at>=?)""",
                (now, job["job_id"], 'browser_operation:' + job["job_id"], now)).rowcount
            if changed:
                for item_id in broken:
                    db.execute("UPDATE job_items SET status='pending', updated_at=?, finished_at=NULL WHERE item_id=?", (now, item_id))
                self.store._add_event_db(db, job["job_id"], "document_retry_pending", {"replayed": False})

    def job(self, job_id: str) -> dict[str, Any]:
        if self.store is None:
            raise ServiceUnavailable("Servicio no disponible. Ejecute: cbrs service start worker")
        job = self.store.get_job(job_id)
        if job is None:
            raise InvalidInscription("job_id desconocido")
        return job

    def pdf(self, job: dict[str, Any]) -> Path | None:
        from .jobs import validate_pdf
        for public in self.store.artifacts(job_id=job["job_id"]):
            record = self.store.artifact_record(public["artifact_id"])
            if not record or not record["valid"]:
                continue
            path = Path(record["path"])
            try:
                digest, _ = validate_pdf(path, expected_pages=record["page_count"])
                if digest == record["sha256"]:
                    return path.resolve()
            except (OSError, ValueError, RuntimeError):
                continue
        return None


class Client:
    """Access the local durable worker. Default wait is 300 seconds per call.

    get/get_batch return Result values for portal outcomes. Invalid input and
    service outages raise typed exceptions. No browser is launched by this API.
    """

    def __init__(self, *, timeout: float = 300, output_dir: str | Path | None = None) -> None:
        self.timeout = _timeout(timeout)
        self.output_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else None
        self._backend: Any = None

    @property
    def _db(self) -> Any:
        if self._backend is None:
            try:
                self._backend = _LocalBackend()
            except ValueError:
                raise InvalidInscription("Configuración inválida; ejecute cbrs config validate") from None
            except (OSError, sqlite3.Error):
                raise ServiceUnavailable("Estado del servicio inaccesible; revise permisos y ejecute cbrs service status") from None
        return self._backend

    def status(self) -> dict[str, Any]:
        """Return service health, configured accounts and durable queue counts."""
        return self._db.status()

    def _result(self, job: dict[str, Any]) -> Result:
        raw = job.get("input") or job.get("request") or {}
        result = Result(raw.get("foja"), raw.get("numero"), raw.get("year", raw.get("ano")),
                        job_id=job["job_id"], account=job.get("account_id"),
                        attempts=len(job.get("attempts", [])), started_at=job.get("started_at"),
                        finished_at=job.get("finished_at"), error=job.get("error_message") or job.get("error_code"))
        if job["status"] == "completed" and job.get("result_count") == 0:
            result.status, result.error = "not_found", "Inscripción inexistente (resultado vacío confirmado)."
        elif job["status"] == "completed":
            result.pdf_path = self._db.pdf(job)
            if result.pdf_path:
                result.status, result.error = "done", None
            else:
                result.status, result.error = "failed", "PDF registrado ausente o inválido; requiere recuperación del documento."
        elif job["status"] in {"failed", "partial", "cancelled"}:
            result.status = "failed"
        else:
            accounts = self.status()["accounts"]
            if accounts and all(account.get("portal_quota") for account in accounts):
                result.status = "pending_quota"
                dates = [account["resume_at"] for account in accounts if account.get("resume_at")]
                result.resume_at = min(dates) if dates else None
                result.error = "Todas las cuentas tienen cuota del portal agotada."
        return result

    def job(self, job_id: str) -> Result:
        """Read a pending or finished job without submitting another search."""
        return self._result(self._db.job(job_id))

    def _output(self, result: Result, output: str | Path | None, *, directory: bool = False) -> Result:
        if result.status != "done" or result.pdf_path is None:
            return result
        default_dir = self.output_dir or getattr(self._db, "export_dir", None)
        destination = output if output is not None else default_dir
        directory = directory or (output is None and default_dir is not None)
        if destination is None:
            return result
        target = Path(destination).expanduser().resolve()
        if directory or target.is_dir() or target.suffix.lower() != ".pdf":
            target = target / f"F{result.fojas}_N{result.numero}_A{result.ano}.pdf"
        if target == result.pdf_path:
            return result
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(result.pdf_path.read_bytes()).digest():
                    raise DownloadFailed("El destino contiene otro archivo; elija un nombre diferente.")
            else:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".cbrs-pdf-", delete=False) as dest:
                        temporary = Path(dest.name)
                        with result.pdf_path.open("rb") as source:
                            shutil.copyfileobj(source, dest)
                        dest.flush()
                        os.fsync(dest.fileno())
                    # Publish an already complete file atomically without overwrite.
                    try:
                        os.link(temporary, target)
                    except FileExistsError:
                        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(temporary.read_bytes()).digest():
                            raise DownloadFailed("El destino contiene otro archivo; elija un nombre diferente.")
                finally:
                    if temporary and temporary.exists():
                        temporary.unlink()
            result.pdf_path = target
        except OSError as exc:
            raise DownloadFailed("No se pudo guardar el PDF en el destino solicitado.") from exc
        return result

    def get(self, *, fojas: int, numero: int, ano: int, output: str | Path | None = None,
            timeout: float | None = None, force: bool = False) -> Result:
        """Submit once and wait; timeout leaves the same durable job pending."""
        values = tuple(_positive(value) for value in (fojas, numero, ano))
        seconds = self.timeout if timeout is None else _timeout(timeout)
        self._db.check()
        job = self._db.submit(values, force)
        deadline = time.monotonic() + seconds
        while True:
            result = self._result(job)
            if result.status != "pending" or time.monotonic() >= deadline:
                return self._output(result, output)
            time.sleep(min(1.0, max(0, deadline - time.monotonic())))
            self._db.check()
            job = self._db.job(result.job_id)

    def get_batch(self, source: str | Path | Iterable[tuple[int, int, int]], *,
                  output_dir: str | Path | None = None, report: str | Path | None = None,
                  no_wait: bool = False, timeout: float | None = None) -> list[Result]:
        """Enqueue every valid row before waiting, preserving duplicates in reports."""
        fields = ["fojas", "numero", "ano"]
        if isinstance(source, (str, Path)):
            if report is not None and Path(report).resolve() == Path(source).resolve():
                raise InvalidInscription("El reporte debe tener una ruta diferente al CSV de entrada.")
            fields, rows = read_csv(source)
        else:
            rows = []
            for index, values in enumerate(source, 1):
                error = None
                try:
                    values = tuple(values)
                    if len(values) != 3:
                        raise InvalidInscription("se requieren tres valores")
                    values = tuple(_positive(value) for value in values)
                except (TypeError, InvalidInscription):
                    values, error = (None, None, None), f"fila {index}: inscripción inválida"
                rows.append((dict(zip(fields, values)), values, error))
        seconds = math.inf if timeout is None else _timeout(timeout)
        self._db.check()
        results = []
        for raw, values, error in rows:
            if error:
                result = Result(*values, status="failed", error=error)
            else:
                result = self._result(self._db.submit(values, False))
            result.source = raw
            results.append(result)
        deadline = time.monotonic() + seconds
        while not no_wait and any(result.status == "pending" for result in results) and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0, deadline - time.monotonic())))
            self._db.check()
            for index, previous in enumerate(results):
                if previous.status == "pending":
                    current = self.job(previous.job_id)
                    current.source = previous.source
                    results[index] = current
        for result in results:
            self._output(result, output_dir, directory=True)
        if report is not None:
            write_report(report, fields, results)
        return results


def write_report(path: str | Path, fields: list[str], results: list[Result]) -> None:
    """Atomically replace a UTF-8 CSV report, preserving source column order."""
    target = Path(path).expanduser().resolve()
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=target.parent,
                                         prefix=".cbrs-report-", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields + list(REPORT_FIELDS))
            writer.writeheader()
            for result in results:
                data = result.to_dict()
                writer.writerow({**result.source, **{key: data[key] for key in REPORT_FIELDS}})
        os.replace(temporary, target)
    except OSError as exc:
        raise DownloadFailed("No se pudo escribir el reporte CSV.") from exc
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
