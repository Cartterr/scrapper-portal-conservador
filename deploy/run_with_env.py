#!/usr/bin/env python3
"""Execute one command with a dotenv file without evaluating it as shell code."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values


ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("env_file", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        parser.error("a command is required after --")
    values = dotenv_values(args.env_file)
    environment = dict(os.environ)
    for key, value in values.items():
        if not ENV_KEY.fullmatch(key):
            raise ValueError("environment file contains an invalid key")
        if value is not None:
            environment[key] = str(value)
    if os.name == "nt":
        return subprocess.run(command, env=environment, check=False).returncode
    os.execvpe(command[0], command, environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
