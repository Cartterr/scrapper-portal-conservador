"""RUT supply for registrations, backed by ``rut_data.db``.

Status lifecycle of a row in ``ruts``::

    scraped -> reserved -> registered          (account fully created)
                     \\-> failed:<stage>        (terminal failure)
                     \\-> scraped               (released after a retryable failure)

Company-looking names are skipped: the registration form is for natural
persons (nombres / apellido paterno / apellido materno).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

COMPANY_MARKERS = (
    "SPA",
    "LTDA",
    "LIMITADA",
    "EIRL",
    "E.I.R.L",
    "S.A.",
    "COMERCIAL",
    "INVERSIONES",
    "SERVICIOS",
    "TRANSPORTES",
    "CONSTRUCTORA",
    "INMOBILIARIA",
    "FUNDACION",
    "CORPORACION",
    "COOPERATIVA",
    "EMPRESA",
    "COMPAÑIA",
    "COMPANIA",
    "ASOCIACION",
    "CLUB",
    "SINDICATO",
)


@dataclass(frozen=True)
class RutRecord:
    id: str | int
    rut: str
    name: str


@dataclass(frozen=True)
class PersonIdentity:
    """A natural person parsed out of a directory row."""

    rut: str
    nombres: str
    apellido_paterno: str
    apellido_materno: str | None

    @property
    def nombre_completo(self) -> str:
        parts = [self.nombres, self.apellido_paterno]
        if self.apellido_materno:
            parts.append(self.apellido_materno)
        return " ".join(part for part in parts if part)


def is_company_name(name: str) -> bool:
    upper = " ".join((name or "").upper().split())
    if not upper:
        return True
    return any(marker in upper for marker in COMPANY_MARKERS)


def parse_person_name(name: str) -> tuple[str, str, str | None]:
    """Split "ApellidoPaterno ApellidoMaterno Nombres..." into form fields.

    The directory stores names as ``Sanchez Sepulveda Elsa Nieves``: two
    surnames first, then the given names.  With only two tokens the second is
    treated as the given name and the maternal surname is left empty (the
    portal treats ``apellidoMaterno`` as optional).
    """

    tokens = [token for token in re.split(r"\s+", (name or "").strip()) if token]
    if len(tokens) >= 3:
        return " ".join(tokens[2:]), tokens[0], tokens[1]
    if len(tokens) == 2:
        return tokens[1], tokens[0], None
    raise ValueError(f"Cannot parse a person name from {name!r}")


class NoRutsAvailable(RuntimeError):
    """Every scraped RUT in the database is already consumed."""


class RutProvider:
    """Reserves and consumes RUT rows from ``rut_data.db`` atomically."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise RuntimeError(f"RUT database not found: {self.db_path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def reserve_next(self) -> RutRecord:
        """Atomically move the oldest usable ``scraped`` row to ``reserved``."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, rut, name FROM ruts WHERE status = 'scraped' "
                "ORDER BY rowid ASC LIMIT 25"
            ).fetchall()
            for row_id, rut, name in rows:
                if is_company_name(name or ""):
                    conn.execute(
                        "UPDATE ruts SET status = 'skipped_company' WHERE id = ?",
                        (row_id,),
                    )
                    continue
                try:
                    parse_person_name(name or "")
                except ValueError:
                    conn.execute(
                        "UPDATE ruts SET status = 'skipped_unparsable' WHERE id = ?",
                        (row_id,),
                    )
                    continue
                cursor = conn.execute(
                    "UPDATE ruts SET status = 'reserved' "
                    "WHERE id = ? AND status = 'scraped'",
                    (row_id,),
                )
                if cursor.rowcount == 1:
                    conn.commit()
                    return RutRecord(id=row_id, rut=str(rut), name=str(name))
            conn.commit()
        raise NoRutsAvailable(
            "No scraped RUTs left in the database; fetch or insert more first."
        )

    def mark_registered(self, rut: str) -> None:
        self._set_status(rut, "registered")

    def mark_failed(self, rut: str, stage: str) -> None:
        safe_stage = re.sub(r"[^a-z0-9_]+", "_", str(stage).lower())[:40]
        self._set_status(rut, f"failed:{safe_stage}")

    def release(self, rut: str) -> None:
        """Return a reserved RUT to the pool after a retryable failure."""

        self._set_status(rut, "scraped", only_from="reserved")

    def _set_status(self, rut: str, status: str, *, only_from: str | None = None) -> None:
        with self._connect() as conn:
            if only_from:
                conn.execute(
                    "UPDATE ruts SET status = ? WHERE rut = ? AND status = ?",
                    (status, rut, only_from),
                )
            else:
                conn.execute(
                    "UPDATE ruts SET status = ? WHERE rut = ?", (status, rut)
                )
            conn.commit()

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM ruts GROUP BY status"
            ).fetchall()
        return {str(status): int(count) for status, count in rows}
