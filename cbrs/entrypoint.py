"""Installed console entry point, shared with python -m cbrs."""
from __future__ import annotations

import sys


def main() -> int:
    """Dispatch public commands without browser imports or runtime setup."""
    if len(sys.argv) > 1 and sys.argv[1] in {"get", "get-batch", "status"}:
        from .download_cli import PublicArgumentParser, add_download_parsers, run
        parser = PublicArgumentParser(prog="cbrs")
        add_download_parsers(parser.add_subparsers(dest="command", required=True))
        return run(parser.parse_args())
    import os
    from .paths import prepare_environment
    os.environ.update(prepare_environment(dict(os.environ)))
    from .cli import main as legacy_main
    return legacy_main()
