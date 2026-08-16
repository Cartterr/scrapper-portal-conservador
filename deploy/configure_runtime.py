#!/usr/bin/env python3
"""Safely materialize protected CBRS runtime configuration from stdin.

This helper is intentionally non-interactive. The Windows installer collects
secrets without echoing them and sends one JSON document through stdin, keeping
credentials out of process arguments, logs, and the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from dotenv import dotenv_values


ACCOUNT_ENV_PATTERN = re.compile(
    r"^CBRS_ACCOUNT_\d+_(?:USERNAME|PASSWORD|PROXY_URL)$"
)
ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
MAX_INPUT_BYTES = 2 * 1024 * 1024


def _single_line(value: object, label: str, *, allow_empty: bool = False) -> str:
    text = str(value or "")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise ValueError(f"{label} must be one printable line")
    if not allow_empty and not text:
        raise ValueError(f"{label} is required")
    return text


def validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_accounts = payload.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("at least one account is required")
    if len(raw_accounts) > 50:
        raise ValueError("at most 50 accounts may be configured in one installation")

    accounts: list[dict[str, Any]] = []
    account_ids: set[str] = set()
    proxy_references: set[str] = set()
    profile_dirs: set[str] = set()
    for index, raw in enumerate(raw_accounts, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"account {index} must be an object")
        account_id = _single_line(raw.get("id") or f"ejecutivo_{index}", "account id")
        if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
            raise ValueError(f"account {index} id must contain only letters, digits, or underscores")
        if account_id in account_ids:
            raise ValueError("account ids must be unique")
        account_ids.add(account_id)

        username = _single_line(raw.get("username"), f"{account_id} username")
        password = _single_line(raw.get("password"), f"{account_id} password")
        proxy_url = _single_line(raw.get("proxy_url"), f"{account_id} proxy URL")
        parsed_proxy = urlparse(proxy_url)
        if parsed_proxy.scheme.lower() not in {"http", "https"}:
            raise ValueError(f"{account_id} proxy URL must use http:// or https://")
        try:
            proxy_port = parsed_proxy.port
        except ValueError as exc:
            raise ValueError(f"{account_id} proxy port is invalid") from exc
        if not parsed_proxy.hostname or proxy_port is None:
            raise ValueError(f"{account_id} proxy URL must include host and port")

        try:
            quota = int(raw.get("daily_quota", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{account_id} daily quota must be an integer") from exc
        if not 1 <= quota <= 10_000:
            raise ValueError(f"{account_id} daily quota must be between 1 and 10000")

        username_env = f"CBRS_ACCOUNT_{index}_USERNAME"
        password_env = f"CBRS_ACCOUNT_{index}_PASSWORD"
        proxy_url_env = f"CBRS_ACCOUNT_{index}_PROXY_URL"
        if proxy_url_env in proxy_references:
            raise ValueError("proxy environment references must be unique")
        proxy_references.add(proxy_url_env)
        profile_dir = f"/var/lib/cbrs/accounts/{account_id}/chrome-profile"
        if profile_dir in profile_dirs:
            raise ValueError("profile directories must be unique")
        profile_dirs.add(profile_dir)

        egress_group = _single_line(
            raw.get("egress_group"), f"{account_id} egress group", allow_empty=True
        )
        if egress_group and not ACCOUNT_ID_PATTERN.fullmatch(egress_group):
            raise ValueError(
                f"{account_id} egress group must contain only letters, digits, or underscores"
            )
        accounts.append(
            {
                "id": account_id,
                "label": _single_line(
                    raw.get("label") or f"Ejecutivo {index}",
                    f"{account_id} label",
                ),
                "username": username,
                "password": password,
                "proxy_url": proxy_url,
                "username_env": username_env,
                "password_env": password_env,
                "proxy_url_env": proxy_url_env,
                "profile_dir": profile_dir,
                "daily_quota": quota,
                "egress_group": egress_group or None,
            }
        )

    raw_backup = payload.get("backup")
    if not isinstance(raw_backup, Mapping):
        raise ValueError("backup configuration is required")
    repository = _single_line(raw_backup.get("repository"), "restic repository")
    restic_password = _single_line(raw_backup.get("password"), "restic password")
    password_file = _single_line(
        raw_backup.get("password_file") or "/etc/cbrs/restic-password",
        "restic password file",
    )
    if not password_file.startswith("/"):
        raise ValueError("restic password file must be an absolute Ubuntu path")

    return {
        "accounts": accounts,
        "backup": {
            "repository": repository,
            "password": restic_password,
            "password_file": password_file,
        },
    }


def build_environment(
    *, template_path: Path, existing_path: Path, validated: Mapping[str, Any]
) -> dict[str, str]:
    environment = {
        key: str(value)
        for key, value in dotenv_values(template_path).items()
        if value is not None
    }
    if existing_path.is_file():
        environment.update(
            {
                key: str(value)
                for key, value in dotenv_values(existing_path).items()
                if value is not None
            }
        )
    for key in list(environment):
        if ACCOUNT_ENV_PATTERN.fullmatch(key):
            del environment[key]

    environment.update(
        {
            "CBRS_BROWSER_BACKEND": "chrome",
            "CBRS_BROWSER_EXECUTABLE_PATH": "/usr/bin/google-chrome-stable",
            "CBRS_HEADLESS": "0",
            "CBRS_WINDOW_MODE": "normal",
            "CBRS_EGRESS_MODE": "dedicated_static_isp",
            "CBRS_EXPECTED_EGRESS_COUNTRY": "CL",
            "CBRS_PROFILE_DIR": "/var/lib/cbrs/chrome-profile",
            "CBRS_OUTPUT_DIR": "/var/lib/cbrs/outputs",
            "CBRS_LOG_DIR": "/var/log/cbrs",
            "CBRS_NOVNC_URL": "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale",
            "DISPLAY": ":99",
        }
    )
    for account in validated["accounts"]:
        environment[account["username_env"]] = account["username"]
        environment[account["password_env"]] = account["password"]
        environment[account["proxy_url_env"]] = account["proxy_url"]
    backup = validated["backup"]
    environment["RESTIC_REPOSITORY"] = backup["repository"]
    environment["RESTIC_PASSWORD_FILE"] = backup["password_file"]
    return environment


def build_pool_config(validated: Mapping[str, Any]) -> dict[str, Any]:
    accounts = []
    for account in validated["accounts"]:
        item = {
            "id": account["id"],
            "label": account["label"],
            "username_env": account["username_env"],
            "password_env": account["password_env"],
            "proxy_url_env": account["proxy_url_env"],
            "profile_dir": account["profile_dir"],
            "daily_quota": account["daily_quota"],
        }
        if account["egress_group"]:
            item["egress_group"] = account["egress_group"]
        accounts.append(item)
    return {
        "daily_quota_per_account": 20,
        "interval_minutes": 0,
        "job_interval_min_seconds": 10,
        "job_interval_max_seconds": 30,
        "dashboard_host": "127.0.0.1",
        "dashboard_port": 8765,
        "accounts": accounts,
    }


def _dotenv_text(environment: Mapping[str, str]) -> str:
    lines = []
    for key, value in environment.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid environment key: {key}")
        lines.append(f"{key}={json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str, *, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".installer.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def apply_configuration(
    *, payload: Mapping[str, Any], app_dir: Path, etc_dir: Path, state_dir: Path
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise PermissionError("configure_runtime.py must run as root")
    service = pwd.getpwnam("cbrs")
    validated = validate_payload(payload)
    env_path = etc_dir / "cbrs.env"
    template_path = app_dir / "deploy" / "cbrs.env.example"
    environment = build_environment(
        template_path=template_path,
        existing_path=env_path,
        validated=validated,
    )
    pool = build_pool_config(validated)
    password_path = Path(validated["backup"]["password_file"])

    _atomic_write(
        env_path,
        _dotenv_text(environment),
        mode=0o640,
        uid=0,
        gid=service.pw_gid,
    )
    _atomic_write(
        state_dir / "account-pool.json",
        json.dumps(pool, ensure_ascii=False, indent=2) + "\n",
        mode=0o640,
        uid=service.pw_uid,
        gid=service.pw_gid,
    )
    _atomic_write(
        password_path,
        validated["backup"]["password"] + "\n",
        mode=0o640,
        uid=0,
        gid=service.pw_gid,
    )

    repository = validated["backup"]["repository"]
    if repository.startswith("/"):
        repository_path = Path(repository)
        repository_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chown(repository_path, service.pw_uid, service.pw_gid)
        except PermissionError:
            # DrvFS or a managed mount can reject chown while still allowing access.
            pass

    return {
        "ok": True,
        "accounts_configured": len(validated["accounts"]),
        "capacity_per_day": sum(
            int(account["daily_quota"]) for account in validated["accounts"]
        ),
        "secrets_printed": False,
    }


def _existing_account_values(
    *, environment: Mapping[str, str | None], pool_path: Path
) -> dict[str, dict[str, Any]]:
    if not pool_path.is_file():
        return {}
    raw_pool = json.loads(pool_path.read_text(encoding="utf-8"))
    values: dict[str, dict[str, Any]] = {}
    for account in raw_pool.get("accounts", []):
        if not isinstance(account, Mapping):
            continue
        account_id = str(account.get("id") or "")
        if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
            continue
        values[account_id] = {
            "username": environment.get(str(account.get("username_env") or "")) or "",
            "password": environment.get(str(account.get("password_env") or "")) or "",
            "proxy_url": environment.get(str(account.get("proxy_url_env") or "")) or "",
            "daily_quota": account.get("daily_quota", 20),
            "label": account.get("label") or account_id,
            "egress_group": account.get("egress_group") or "",
        }
    return values


def build_account_update_payload(
    update: Mapping[str, Any], *, existing_env_path: Path, state_dir: Path
) -> dict[str, Any]:
    """Merge a local dashboard account update without returning old secrets."""
    raw_accounts = update.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError("at least one account is required")
    existing_environment = dotenv_values(existing_env_path)
    existing = _existing_account_values(
        environment=existing_environment,
        pool_path=state_dir / "account-pool.json",
    )
    accounts: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_accounts, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"account {index} must be an object")
        account_id = _single_line(raw.get("id"), "account id")
        prior = existing.get(account_id, {})
        accounts.append(
            {
                "id": account_id,
                "label": raw.get("label") or prior.get("label") or account_id,
                "username": raw.get("username") or prior.get("username"),
                "password": raw.get("password") or prior.get("password"),
                "proxy_url": raw.get("proxy_url") or prior.get("proxy_url"),
                "daily_quota": raw.get("daily_quota", prior.get("daily_quota", 20)),
                "egress_group": raw.get("egress_group") or prior.get("egress_group") or "",
            }
        )
    repository = existing_environment.get("RESTIC_REPOSITORY") or ""
    password_file = existing_environment.get("RESTIC_PASSWORD_FILE") or "/etc/cbrs/restic-password"
    try:
        restic_password = Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise ValueError("existing backup password is unavailable") from exc
    return validate_payload(
        {
            "accounts": accounts,
            "backup": {
                "repository": repository,
                "password": restic_password,
                "password_file": password_file,
            },
        }
    )


def apply_account_update_request(
    *, request_path: Path, app_dir: Path, etc_dir: Path, state_dir: Path
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise PermissionError("configure_runtime.py must run as root")
    raw = request_path.read_bytes()
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("account configuration request is invalid")
    update = json.loads(raw.decode("utf-8"))
    if not isinstance(update, Mapping):
        raise ValueError("account configuration request must be an object")
    validated = build_account_update_payload(
        update,
        existing_env_path=etc_dir / "cbrs.env",
        state_dir=state_dir,
    )
    result = apply_configuration(
        payload={
            "accounts": validated["accounts"],
            "backup": {
                "repository": dotenv_values(etc_dir / "cbrs.env").get("RESTIC_REPOSITORY") or "",
                "password": Path(validated["backup"]["password_file"]).read_text(encoding="utf-8").rstrip("\r\n"),
                "password_file": validated["backup"]["password_file"],
            },
        },
        app_dir=app_dir,
        etc_dir=etc_dir,
        state_dir=state_dir,
    )
    request_path.unlink(missing_ok=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", type=Path, default=Path("/opt/cbrs"))
    parser.add_argument("--etc-dir", type=Path, default=Path("/etc/cbrs"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/cbrs"))
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--update-accounts-only", action="store_true")
    parser.add_argument(
        "--request-file", type=Path, default=Path("/var/lib/cbrs/control/account-configuration.json")
    )
    args = parser.parse_args(argv)
    if args.update_accounts_only:
        result = apply_account_update_request(
            request_path=args.request_file,
            app_dir=args.app_dir,
            etc_dir=args.etc_dir,
            state_dir=args.state_dir,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("configuration payload is too large")
    payload = json.loads(raw.decode("utf-8"))
    validated = validate_payload(payload)
    if args.validate_only:
        result = {
            "ok": True,
            "accounts_configured": len(validated["accounts"]),
            "capacity_per_day": sum(
                int(account["daily_quota"]) for account in validated["accounts"]
            ),
            "secrets_printed": False,
        }
    else:
        result = apply_configuration(
            payload=payload,
            app_dir=args.app_dir,
            etc_dir=args.etc_dir,
            state_dir=args.state_dir,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc), "secrets_printed": False},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
