"""Trusted local, immutable application releases; never reload browser owners.

Only the owning thread activates at an operation boundary. No exec endpoint,
remote code, importlib.reload, automatic search replay, or object replacement.
"""
from __future__ import annotations

import ast
import contextvars
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
import types
import uuid

ABI = 1
MODULES = ("error_evidence", "pdf", "form_search", "runtime_observation", "runtime_logic")
CONTRACTS = {"error_evidence": "capture_error", "pdf": "create_pdf",
             "form_search": "search_fna_form", "runtime_observation": "sample_auth",
             "runtime_logic": "process_job"}
_active = contextvars.ContextVar("cbrs_runtime_generation", default=None)


def runtime_module(name):
    if name not in MODULES:
        raise ValueError("Unsupported runtime component")
    generation = _active.get()
    return generation[name] if generation else importlib.import_module(f"cbrs.{name}")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def core_fingerprint(source):
    # Core includes schemas, safety limits, browser ownership, dependencies and
    # adapter contracts. Changes there require deliberate migration, not reload.
    files = {p.name: _digest(p.read_bytes()) for p in sorted(Path(source).glob("*.py"))
             if p.stem not in MODULES}
    for path in sorted(Path(source).parent.glob("requirements*.txt")):
        files[path.name] = _digest(path.read_bytes())
    files["python"] = sys.version
    return _digest(json.dumps(files, sort_keys=True).encode())


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def publish(source, root):
    """Snapshot checked source only; excludes profiles, environment and secrets."""
    source, root = Path(source), Path(root)
    stable = core_fingerprint(source)
    payloads = {name: (source / f"{name}.py").read_bytes() for name in MODULES}
    hashes = {name: _digest(data) for name, data in payloads.items()}
    for name, data in payloads.items():
        tree = ast.parse(data, filename=f"{name}.py")
        if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == CONTRACTS[name] for node in tree.body):
            raise ValueError("Missing runtime contract")
        compile(tree, f"{name}.py", "exec")
    manifest = {"abi": ABI, "core": stable, "files": hashes}
    release = _digest(json.dumps(manifest, sort_keys=True).encode())
    manifest["release"] = release
    directory = root / "releases" / release
    if directory.exists():
        for name, data in payloads.items():
            if (directory / f"{name}.py").read_bytes() != data:
                raise ValueError("Immutable release was modified")
    else:
        stage = root / "releases" / (".stage-" + uuid.uuid4().hex)
        stage.mkdir(parents=True)
        for name, data in payloads.items():
            (stage / f"{name}.py").write_bytes(data)
        _atomic_json(stage / "manifest.json", manifest)
        stage.rename(directory)
    if stable != core_fingerprint(source):
        raise ValueError("Core changed during publication; retry after editing")
    request(root, release)
    return release


def request(root, release):
    if release != "builtin" and not re.fullmatch(r"[a-f0-9]{64}", release):
        raise ValueError("Invalid release identifier")
    if release != "builtin" and not (Path(root) / "releases" / release / "manifest.json").is_file():
        raise ValueError("Unknown release")
    _atomic_json(Path(root) / "desired.json", {"release": release, "request": uuid.uuid4().hex})


def read_status(root):
    try:
        data = json.loads((Path(root) / "status.json").read_text(encoding="utf-8"))
        command = Path(root) / "desired.json"
        desired = json.loads(command.read_text(encoding="utf-8")) if command.exists() else {}
        result = {key: data.get(key) for key in ("state", "active_release", "previous_release", "owner", "checked_at", "error", "activation_boundary")}
        requested = desired.get("release")
        result["pending"] = bool(requested and requested != data.get("active_release") and
                                 (data.get("state") != "rejected" or requested != data.get("desired_release")))
        return result
    except (OSError, ValueError, TypeError):
        return {"state": "not_available", "pending": False}


class RuntimeUpdates:
    def __init__(self, source, root, *, owner):
        self.source, self.root, self.owner = Path(source), Path(root), owner
        self.core = core_fingerprint(self.source)
        self.release = "builtin"
        self.previous = None
        self.last_request = None
        self.namespace = None
        self.builtin = {name: importlib.import_module(f"cbrs.{name}") for name in MODULES}
        self.token = _active.set(self.builtin)
        self._status("ready")

    def _status(self, state, *, desired=None, error=None):
        _atomic_json(self.root / "status.json", {
            "abi": ABI, "state": state, "active_release": self.release,
            "previous_release": self.previous, "desired_release": desired,
            "owner": self.owner, "checked_at": time.time(), "error": error,
            "activation_boundary": "between_operations",
        })

    def _load(self, release):
        directory = self.root / "releases" / release
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        identity = {k: manifest[k] for k in ("abi", "core", "files")}
        if _digest(json.dumps(identity, sort_keys=True).encode()) != release:
            raise ValueError("release_integrity_failed")
        if manifest["abi"] != ABI or manifest["core"] != self.core:
            raise ValueError("core_migration_required")
        if set(manifest["files"]) != set(MODULES):
            raise ValueError("invalid_component_set")
        sources = {}
        for name in MODULES:
            data = (directory / f"{name}.py").read_bytes()
            if len(data) > 2_000_000 or _digest(data) != manifest["files"][name]:
                raise ValueError("release_integrity_failed")
            sources[name] = data
        namespace = f"cbrs._release_{release}_{uuid.uuid4().hex}"
        package = types.ModuleType(namespace)
        package.__path__ = []
        sys.modules[namespace] = package
        result = {}
        try:
            # Share stable classes/errors/contracts; do not create parallel
            # SafetyStopException or Settings identities in a new package.
            for data in sources.values():
                for node in ast.walk(ast.parse(data)):
                    if isinstance(node, ast.ImportFrom) and node.level:
                        if node.level != 1 or not node.module or "." in node.module:
                            raise ValueError("unsupported_relative_import")
                        if node.module not in MODULES:
                            sys.modules[f"{namespace}.{node.module}"] = importlib.import_module(f"cbrs.{node.module}")
            for name in MODULES:
                module = types.ModuleType(f"{namespace}.{name}")
                module.__package__ = namespace
                module.__file__ = str(directory / f"{name}.py")
                sys.modules[module.__name__] = module
                exec(compile(sources[name], module.__file__, "exec"), module.__dict__)
                if not callable(getattr(module, CONTRACTS[name], None)):
                    raise ValueError("invalid_component_contract")
                if name == "runtime_observation" and not callable(getattr(module, "preview_interval", None)):
                    raise ValueError("invalid_component_contract")
                result[name] = module
            return result, namespace
        except Exception:
            self._unload(namespace)
            raise

    @staticmethod
    def _unload(namespace):
        if namespace:
            for name in tuple(sys.modules):
                if name == namespace or name.startswith(namespace + "."):
                    sys.modules.pop(name, None)

    def poll(self):
        """Call ONLY from the owning thread between complete operations."""
        path = self.root / "desired.json"
        if not path.exists():
            return False
        desired = None
        try:
            raw = path.read_bytes()
            key = _digest(raw)
            if key == self.last_request:
                return False
            self.last_request = key
            command = json.loads(raw)
            desired = command["release"]
            if desired == self.release:
                self._status("active", desired=desired)
                return False
            if desired == "builtin":
                candidate, namespace = self.builtin, None
            elif re.fullmatch(r"[a-f0-9]{64}", desired):
                candidate, namespace = self._load(desired)
            else:
                raise ValueError("invalid_release")
            old_namespace = self.namespace
            _active.set(candidate)
            self.previous, self.release = self.release, desired
            self.namespace = namespace
            self._status("active", desired=desired)
            self._unload(old_namespace)
            return True
        except Exception as exc:
            # Never log exception payloads from executable code or replay a job.
            reason = str(exc) if str(exc) in {"core_migration_required", "release_integrity_failed"} else "release_rejected"
            self._status("rejected", desired=desired if isinstance(desired, str) and re.fullmatch(r"[a-f0-9]{64}", desired) else None, error=reason)
            return False

    def close(self):
        _active.reset(self.token)
        self._unload(self.namespace)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Publish trusted local runtime code at safe boundaries")
    parser.add_argument("action", choices=("publish", "status", "rollback"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--source", default=str(Path(__file__).parent))
    parser.add_argument("--release", default="builtin")
    args = parser.parse_args(argv)
    if args.action == "publish":
        print(json.dumps({"requested_release": publish(args.source, args.root)}))
    elif args.action == "rollback":
        request(args.root, args.release)
        print(json.dumps({"requested_release": args.release}))
    else:
        path = Path(args.root) / "status.json"
        print(path.read_text() if path.exists() else '{"state":"not_started"}')


if __name__ == "__main__":
    main()
