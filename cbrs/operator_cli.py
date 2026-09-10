"""Compact, colorful operator console for the Linux/WSL CBRS service."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config
from .paths import REPO_ROOT


RESET = "\033[0m"
COLORS = {
    "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m",
    "red": "\033[31m", "blue": "\033[34m", "dim": "\033[2m", "bold": "\033[1m",
}
SERVICE_UNITS = {
    "owner": "cbrs-browser-owner.service",
    "worker": "cbrs-worker.service",
    "dashboard": "cbrs-dashboard.service",
    "display": "cbrs-display.service",
    "novnc": "cbrs-novnc.service",
    "backup": "cbrs-backup.timer",
    "watchdog": "cbrs-watchdog.timer",
}
SESSION_UNITS = {"owner", "display", "novnc"}
SECRET_WORDS = ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "PROXY_URL", "LOGIN")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


class Paint:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def __call__(self, text: object, color: str) -> str:
        value = str(text)
        return f"{COLORS[color]}{value}{RESET}" if self.enabled else value


def _paint(args) -> Paint:
    return Paint(not getattr(args, "no_color", False) and (sys.stdout.isatty() or os.environ.get("FORCE_COLOR")))


def _run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def _unit_state(unit: str) -> dict[str, str]:
    if not sys.platform.startswith("linux"):
        return {"active": "unsupported", "enabled": "unsupported", "detail": "Linux/WSL only"}
    show = _run(["systemctl", "show", unit, "--property=ActiveState,SubState,UnitFileState", "--no-pager"])
    values = {}
    for line in show.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return {
        "active": values.get("ActiveState", "unknown"),
        "detail": values.get("SubState", show.stderr.strip() or "unknown"),
        "enabled": values.get("UnitFileState", "unknown"),
    }


def _snapshot() -> dict[str, Any]:
    from .account_pool import AccountPoolStore, dashboard_status, load_account_pool_config
    from .captcha_budget import CaptchaBudgetStore
    from .endurance import EnduranceController, load_endurance_plan
    from .jobs import default_job_store

    settings = config.SETTINGS
    store = default_job_store(settings)
    pool_config = load_account_pool_config(settings)
    pool_store = AccountPoolStore(store.path)
    plan = load_endurance_plan(settings.profile_dir.parent / "endurance-plan.json")
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "services": {name: _unit_state(unit) for name, unit in SERVICE_UNITS.items()},
        "jobs": store.summary(),
        "recent_jobs": store.list_jobs(limit=5),
        "pool": dashboard_status(pool_store, config=pool_config),
        "endurance": EnduranceController(store, plan, pool_config).status(),
        "captcha": CaptchaBudgetStore(
            settings.captcha_state_path,
            daily_limit=settings.two_captcha_daily_limit,
            circuit_seconds=settings.two_captcha_circuit_breaker_seconds,
            rejection_cooldown_seconds=settings.two_captcha_rejection_cooldown_seconds,
        ).status(),
    }


def _short(value: Any, width: int = 24) -> str:
    text = "-" if value in (None, "") else str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def _status(value: str, p: Paint) -> str:
    color = "green" if value in {"active", "available", "completed", "passed", "running"} else (
        "yellow" if value in {"inactive", "queued", "paused", "not_started", "cooldown"} else "red"
    )
    return p(value, color)


def render_overview(data: dict[str, Any], *, color: bool = True) -> str:
    p = Paint(color)
    services = data.get("services", {})
    jobs = data.get("jobs", {})
    pool = data.get("pool", {})
    pool_summary = pool.get("pool", {})
    accounts = pool.get("accounts", [])
    lines = [
        f"{p('CBRS', 'bold')} {p('OPERADOR', 'cyan')}  {p(data.get('generated_at', ''), 'dim')}",
        "-" * 72,
        "  ".join(
            f"{name[:4].upper()} {_status(services[name].get('active', 'unknown'), p)}"
            for name in ("owner", "worker", "dashboard", "display")
            if name in services
        ),
        "  ".join(
            f"{name[:4].upper()} {_status(services[name].get('active', 'unknown'), p)}"
            for name in ("novnc", "backup", "watchdog")
            if name in services
        ),
        "",
        f"{p('COLA', 'blue')}  pendientes {p(jobs.get('queued', 0), 'yellow')}  ejecutando {p(jobs.get('running', 0), 'cyan')}  PDFs {p(jobs.get('artifacts', 0), 'green')}",
        f"{p('CUPO', 'blue')}  usado {pool_summary.get('used_today', 0)}/{pool_summary.get('daily_quota', 0)}  disponible {p(pool_summary.get('remaining_today', 0), 'green')}",
        "",
        f"{p('CUENTAS', 'blue')}  {'ESTADO':12} {'USO':7} {'RUTA':12} {'ULTIMO EVENTO'}",
    ]
    for account in accounts[:8]:
        used = account.get("used_today", account.get("daily_used", 0))
        quota = account.get("daily_quota", "-")
        event = account.get("last_event") or account.get("detail") or "-"
        if isinstance(event, dict):
            event = event.get("message", "-")
        lines.append(
            f"  {_short(account.get('account_id'), 14):14} {_status(str(account.get('status', 'unknown')), p):23} "
            f"{str(used) + '/' + str(quota):7} {_short(account.get('proxy_provider') or account.get('egress_group'), 12):12} {_short(event, 24)}"
        )
    lines.extend(["", f"{p('TRABAJOS RECIENTES', 'blue')}  {'ESTADO':12} {'CUENTA':14} {'ID'}"])
    for job in data.get("recent_jobs", []):
        lines.append(
            f"  {_status(str(job.get('status', 'unknown')), p):23} {_short(job.get('current_account_id'), 14):14} {_short(job.get('job_id'), 30)}"
        )
    cooldown = jobs.get("global_safety_cooldown")
    if cooldown:
        lines.extend(["", p(f"ALERTA pausa de seguridad: {_short(cooldown.get('reason'), 48)}", "yellow")])
    lines.extend(["", p("Actualizacion: cbrs overview --watch   Ayuda: cbrs commands", "dim")])
    return "\n".join(lines)


def cmd_overview(args) -> int:
    while True:
        try:
            payload = _snapshot()
        except Exception as exc:  # operator output must remain concise
            print(f"No se pudo leer el estado: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.watch:
            print("\033[2J\033[H", end="")
        print(render_overview(payload, color=_paint(args).enabled))
        if not args.watch:
            return 0
        try:
            time.sleep(max(1.0, args.interval))
        except KeyboardInterrupt:
            print("\nMonitor detenido.")
            return 0


def cmd_services(args) -> int:
    names = list(SERVICE_UNITS) if args.component == "all" else [args.component]
    if args.service_action == "status":
        payload = {name: _unit_state(SERVICE_UNITS[name]) for name in names}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            p = _paint(args)
            for name, state in payload.items():
                print(f"{name:10} {_status(state['active'], p):18} {state['detail']:12} {p(state['enabled'], 'dim')}")
        return 0 if all(state["active"] == "active" for state in payload.values()) else 1
    if args.service_action == "logs":
        command = ["journalctl"]
        for name in names:
            command.extend(["-u", SERVICE_UNITS[name]])
        command.extend(["-n", str(args.lines), "--no-pager"])
        if args.follow:
            command.append("-f")
        return subprocess.call(command)
    if (
        args.service_action in {"stop", "restart"}
        and any(name in SESSION_UNITS for name in names)
        and not args.acknowledge_session_loss
    ):
        print(
            "Operacion rechazada: owner/display/noVNC protegen sesiones autenticadas. "
            "Use --acknowledge-session-loss solo con autorizacion explicita para detener/reiniciar el servicio completo.",
            file=sys.stderr,
        )
        return 2
    verb = args.service_action
    command = ["systemctl", verb, *[SERVICE_UNITS[name] for name in names]]
    if os.geteuid() != 0:
        command.insert(0, "sudo")
    result = subprocess.run(command)
    if result.returncode == 0:
        print(f"OK: {verb} solicitado para {', '.join(names)}")
    return result.returncode


def _display_value(name: str, value: Any) -> str:
    upper = name.upper()
    if any(word in upper for word in SECRET_WORDS):
        return "configurado" if value else "no configurado"
    return str(value)


def _env_path() -> Path:
    return REPO_ROOT / ".env"


def _atomic_env_update(key: str, value: str | None) -> None:
    path = _env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={value}" if value is not None else None
    output, found = [], False
    for line in lines:
        if line.startswith(f"{key}="):
            found = True
            if replacement is not None:
                output.append(replacement)
        else:
            output.append(line)
    if not found and replacement is not None:
        output.append(replacement)
    temp = path.with_suffix(".env.tmp")
    temp.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.chmod(temp, 0o640)
    temp.replace(path)


def cmd_config(args) -> int:
    if args.config_action == "paths":
        payload = {
            "repository": str(REPO_ROOT), "environment": str(_env_path()),
            "state": str(config.SETTINGS.profile_dir.parent),
            "accounts": str(config.SETTINGS.profile_dir.parent / "account-pool.json"),
            "outputs": str(config.SETTINGS.output_dir), "logs": str(config.SETTINGS.log_dir),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for key, value in payload.items():
                print(f"{key:12} {value}")
        return 0
    if args.config_action == "validate":
        try:
            config.load_settings(root=REPO_ROOT)
            from .account_pool import load_account_pool_config
            pool = load_account_pool_config(config.SETTINGS)
        except Exception as exc:
            print(f"FAIL configuracion: {exc}", file=sys.stderr)
            return 1
        print(f"OK configuracion de servicio - {len(pool.accounts)} cuenta(s)")
        return 0
    if args.config_action in {"set", "unset"}:
        if not ENV_KEY.fullmatch(args.key) or not args.key.startswith("CBRS_"):
            print("La clave debe usar el formato CBRS_NOMBRE.", file=sys.stderr)
            return 2
        _atomic_env_update(args.key, args.value if args.config_action == "set" else None)
        print(f"OK {args.key} {'actualizada' if args.config_action == 'set' else 'eliminada'}.")
        print("Ejecute `cbrs config validate`. Los cambios del worker requieren `cbrs service restart worker`.")
        return 0
    values = dataclasses.asdict(config.SETTINGS)
    payload = {key: _display_value(key, value) for key, value in values.items()}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for key, value in payload.items():
            print(f"{key:40} {value}")
    return 0


def cmd_commands(_args) -> int:
    print("""CBRS - guia rapida

  cbrs overview --watch               panel compacto en tiempo real
  cbrs service status all             estado de todos los servicios
  cbrs service logs worker --follow   logs vivos del worker
  cbrs config show                    configuracion efectiva (secretos ocultos)
  cbrs config validate                validar .env y cuentas
  cbrs accounts                       estado detallado de cuentas
  cbrs jobs list                      trabajos recientes
  cbrs jobs enqueue --text \"EMPRESA\"  agregar busqueda
  cbrs jobs show JOB_ID               detalle y artefactos
  cbrs jobs cancel JOB_ID             cancelar en un punto seguro
  cbrs health                         diagnostico local y servicios

Referencia completa: docs/cli-reference.md
Ayuda de cada comando: cbrs COMANDO --help""")
    return 0


def cmd_accounts(args) -> int:
    payload = _snapshot()["pool"]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    p = _paint(args)
    print(f"{'CUENTA':16} {'ESTADO':20} {'USO':8} {'PROVEEDOR':24} RUTA")
    for account in payload.get("accounts", []):
        used = account.get("used_today", account.get("daily_used", 0))
        quota = account.get("daily_quota", "-")
        print(
            f"{_short(account.get('account_id'), 16):16} {_status(str(account.get('status', 'unknown')), p):29} "
            f"{str(used) + '/' + str(quota):8} {_short(account.get('proxy_provider'), 24):24} {_short(account.get('egress_group'), 18)}"
        )
    return 0


def cmd_health(args) -> int:
    service_result = cmd_services(type("Args", (), {
        "component": "all", "service_action": "status", "json": False,
        "no_color": args.no_color,
    })())
    print()
    from .cli import cmd_doctor
    doctor_result = cmd_doctor()
    return 0 if service_result == 0 and doctor_result == 0 else 1


def add_operator_parsers(subparsers) -> None:
    overview = subparsers.add_parser("overview", help="Compact live service overview")
    overview.add_argument("--watch", "-w", action="store_true", help="Refresh continuously")
    overview.add_argument("--interval", type=float, default=2.0, help="Refresh seconds")
    overview.add_argument("--json", action="store_true", help="Machine-readable snapshot")

    service = subparsers.add_parser("service", help="Inspect and control Linux/WSL services")
    service_sub = service.add_subparsers(dest="service_action", required=True)
    for action in ("status", "start", "stop", "restart"):
        item = service_sub.add_parser(action)
        item.add_argument("component", choices=(*SERVICE_UNITS, "all"))
        item.add_argument("--json", action="store_true")
        item.add_argument("--acknowledge-session-loss", action="store_true", help=argparse.SUPPRESS)
    logs = service_sub.add_parser("logs")
    logs.add_argument("component", choices=(*SERVICE_UNITS, "all"))
    logs.add_argument("--lines", type=int, default=80)
    logs.add_argument("--follow", "-f", action="store_true")

    configuration = subparsers.add_parser("config", help="Inspect or update service configuration")
    config_sub = configuration.add_subparsers(dest="config_action", required=True)
    for action in ("show", "paths", "validate"):
        item = config_sub.add_parser(action)
        item.add_argument("--json", action="store_true")
    setting = config_sub.add_parser("set", help="Atomically set one CBRS_* value in .env")
    setting.add_argument("key")
    setting.add_argument("value")
    unset = config_sub.add_parser("unset", help="Remove one CBRS_* value from .env")
    unset.add_argument("key")

    accounts = subparsers.add_parser("accounts", help="Show account, quota, route and login states")
    accounts.add_argument("--json", action="store_true")
    subparsers.add_parser("health", help="Run service and local configuration diagnostics")
    subparsers.add_parser("commands", help="Show the operator command map")


# argparse is imported late in normal command execution; needed by help suppression above.
import argparse
