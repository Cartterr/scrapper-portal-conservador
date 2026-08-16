from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path


def pytest_configure(config) -> None:
    """Avoid stale/ACL-broken shared temp roots on Windows and network drives."""
    if config.option.basetemp is None:
        config.option.basetemp = Path(tempfile.gettempdir()) / (
            f"cbrs-pytest-{os.getpid()}-{uuid.uuid4().hex}"
        )
