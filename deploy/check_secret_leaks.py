"""Check configured secrets without printing their values or matching lines."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

from dotenv import dotenv_values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--logs", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.env.is_file():
        print("NOT VERIFIED: no hay archivo de credenciales para comparar")
        return 2
    configured = dotenv_values(args.env)
    secrets = {str(value).encode("utf-8") for key, value in configured.items()
               if value and re.search(r"(?:PASSWORD|PROXY_LOGIN|API_KEY)$", key)
               and str(value) != "REPLACE_ME"}
    if not secrets:
        print("NOT VERIFIED: no hay secretos configurados para comparar")
        return 2
    listing = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=root, capture_output=True, check=True)
    files = {root / name.decode("utf-8") for name in listing.stdout.split(b"\0") if name}
    if args.logs:
        if not args.logs.is_dir():
            print("NOT VERIFIED: el directorio de logs no existe")
            return 2
        files.update(path for path in args.logs.rglob("*") if path.is_file())
    matches = unreadable = scanned = 0
    overlap = max(map(len, secrets)) - 1
    for path in files:
        # A tracked .env must be counted as a leak, not silently exempted.
        try:
            with path.open("rb") as stream:
                tail = b""
                found = False
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    data = tail + chunk
                    if any(secret in data for secret in secrets):
                        found = True
                        break
                    tail = data[-overlap:] if overlap else b""
                matches += int(found)
            scanned += 1
        except OSError:
            unreadable += 1
    print(f"archivos={scanned} archivos_con_coincidencias={matches} ilegibles={unreadable} logs_incluidos={bool(args.logs)}")
    return 1 if matches or unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())
