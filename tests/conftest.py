from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path


def pytest_configure(config) -> None:
    """Use a short private scratch tree, including subprocess temps."""
    root = Path(__file__).resolve().parents[1]
    if os.name == "posix":
        # Chrome adds a generated profile name and Unix-domain socket below
        # TMPDIR. Mounted or deeply nested checkouts can otherwise cross the
        # 107-byte Linux socket limit before application code is exercised.
        scratch = Path("/tmp") / f"cbrs-tests-{os.getuid()}"
    else:
        scratch = root / ".cbrs/test-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        scratch.chmod(0o700)
    tempfile.tempdir = str(scratch)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scratch)
    if config.option.basetemp is None:
        config.option.basetemp = scratch / (
            f"cbrs-pytest-{os.getpid()}-{uuid.uuid4().hex}"
        )
