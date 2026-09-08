"""Repository-owned runtime locations; independent of the launch directory."""
from pathlib import Path
import os
import shutil

REPO_ROOT = Path(__file__).resolve().parents[1]
PATH_KEYS = frozenset({
    "CBRS_PROFILE_DIR", "CBRS_CLOAK_PROFILE_DIR", "CBRS_CLOAK_CACHE_DIR",
    "CBRS_OUTPUT_DIR", "CBRS_LOG_DIR", "CBRS_CAPTCHA_STATE_PATH",
    "CBRS_BROWSER_EXECUTABLE_PATH", "CBRS_RESTIC_EXECUTABLE_PATH",
    "RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE", "RESTIC_CACHE_DIR",
})


def runtime_environment(root: Path = REPO_ROOT) -> dict[str, str]:
    state = root.resolve() / ".cbrs" / "runtime"
    binary = state / "bin" / ("restic.exe" if os.name == "nt" else "restic")
    return {
        "CBRS_PROFILE_DIR": str(state / "chrome-profile"),
        "CBRS_CLOAK_PROFILE_DIR": str(state / "cloak-profile"),
        "CBRS_CLOAK_CACHE_DIR": str(state / "cache" / "cloak"),
        "CBRS_OUTPUT_DIR": str(state / "outputs"),
        "CBRS_LOG_DIR": str(state / "logs"),
        "CBRS_CAPTCHA_STATE_PATH": str(state / "pool" / "pool.sqlite3"),
        "RESTIC_REPOSITORY": str(state / "backup" / "restic"),
        "RESTIC_PASSWORD_FILE": str(state / "secrets" / "restic-password"),
        "RESTIC_CACHE_DIR": str(state / "cache" / "restic"),
        "CBRS_RESTIC_EXECUTABLE_PATH": str(binary) if binary.is_file() else (shutil.which("restic") or ""),
        "TMP": str(state / "tmp"), "TEMP": str(state / "tmp"),
        "TMPDIR": str(state / "tmp"),
        "XDG_CACHE_HOME": str(state / "cache"),
        "XDG_CONFIG_HOME": str(state / "config"),
        "XDG_DATA_HOME": str(state / "data"),
        "XDG_STATE_HOME": str(state / "state"),
        "PLAYWRIGHT_BROWSERS_PATH": str(state / "cache" / "playwright"),
    }


def prepare_environment(environment: dict[str, str], root: Path = REPO_ROOT) -> dict[str, str]:
    result = {key: value for key, value in environment.items() if key not in PATH_KEYS}
    result.update(runtime_environment(root))
    for key in ("TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        Path(result[key]).mkdir(parents=True, exist_ok=True)
    return result
