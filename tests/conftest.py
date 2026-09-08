from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path


def pytest_configure(config) -> None:
    """Use a unique repository-local scratch tree, including subprocess temps."""
    scratch = Path(__file__).resolve().parents[1] / ".cbrs/test-tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(scratch)
    if config.option.basetemp is None:
        config.option.basetemp = scratch / (
            f"cbrs-pytest-{os.getpid()}-{uuid.uuid4().hex}"
        )
