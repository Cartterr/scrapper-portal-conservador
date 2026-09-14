"""Thin CLI adapter for the public Client API."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .api import Client, DownloadFailed, InvalidInscription, ServiceUnavailable


class PublicArgumentParser(argparse.ArgumentParser):
    """Keep invalid-argument diagnostics on a single stderr line."""

    def error(self, message: str) -> None:
        self.exit(2, "Error: " + " ".join(message.splitlines()) + "\n")


def add_download_parsers(subparsers) -> None:
    """Register user-facing download commands."""
    get = subparsers.add_parser("get", help="Descargar una inscripción como PDF")
    for name in ("fojas", "numero", "ano"):
        get.add_argument("--" + name, required=True, type=int)
    get.add_argument("--output")
    get.add_argument("--force", action="store_true")
    get.add_argument("--timeout", type=float, default=300)
    get.add_argument("--json", action="store_true")
    batch = subparsers.add_parser("get-batch", help="Descargar inscripciones desde CSV")
    batch.add_argument("input", type=Path)
    batch.add_argument("--output", type=Path)
    batch.add_argument("--report", type=Path, default=Path("resultados.csv"))
    batch.add_argument("--timeout", type=float, default=None)
    batch.add_argument("--no-wait", action="store_true")
    batch.add_argument("--json", action="store_true")
    status = subparsers.add_parser("status", help="Estado del servicio, cuentas y cola")
    status.add_argument("--json", action="store_true")


def run(args) -> int:
    """Convert public API outcomes to the documented output and exit codes."""
    try:
        client = Client(timeout=getattr(args, "timeout", None) if getattr(args, "timeout", None) is not None else 300)
        if args.command == "status":
            data = client.status()
            if args.json:
                print(json.dumps(data, ensure_ascii=False, default=str))
            else:
                print("Servicio: " + ("arriba" if data["service"] else "abajo; ejecute cbrs service start worker"))
                for account in data["accounts"]:
                    print(f"{account['account']}: {account['status']} | cupos estimados {account['remaining_estimated']} | "
                          f"proxy {account['proxy']} | reanudación {account['resume_at'] or '-'} | error {account['error'] or '-'}")
                print(f"Trabajos: {data['jobs']['counts']}")
            return 0 if data["service"] else 2
        if args.command == "get":
            result = client.get(fojas=args.fojas, numero=args.numero, ano=args.ano,
                                output=args.output, force=args.force)
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False))
            elif result.status == "done":
                print(result.pdf_path)
            else:
                print(f"status={result.status} job_id={result.job_id} resume_at={result.resume_at or '-'}")
            if result.status != "done":
                print(" ".join((result.error or f"Trabajo pendiente: {result.job_id}").splitlines()), file=sys.stderr)
            return 0 if result.status == "done" else 1
        if args.report.resolve() == args.input.resolve():
            raise InvalidInscription("El reporte debe tener una ruta diferente al CSV de entrada.")
        results = client.get_batch(args.input, output_dir=args.output, report=args.report,
                                   no_wait=args.no_wait, timeout=args.timeout)
        counts = Counter(result.status for result in results)
        summary = {key: counts[key] for key in ("done", "not_found", "pending_quota", "pending", "failed")}
        summary.update(total=len(results), report=str(args.report.resolve()),
                       job_ids=[result.job_id for result in results],
                       resume_at=min((result.resume_at for result in results if result.resume_at), default=None))
        if args.json:
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print(" | ".join(f"{key}={value}" for key, value in summary.items() if key != "job_ids"))
            if args.no_wait:
                print("job_id: " + ", ".join(job_id for job_id in summary["job_ids"] if job_id))
        return 0 if all(result.status in {"done", "not_found"} for result in results) else 1
    except (InvalidInscription, ServiceUnavailable) as exc:
        print(" ".join(str(exc).splitlines()), file=sys.stderr)
        return 2
    except DownloadFailed as exc:
        print(" ".join(str(exc).splitlines()), file=sys.stderr)
        return 1
