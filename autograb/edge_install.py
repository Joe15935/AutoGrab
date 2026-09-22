"""User-level Edge Native Messaging registration, without profile access."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

HOST_NAME = "com.autograb.edge"


def identity(root: Path) -> str:
    manifest = json.loads((root / "edge-extension/manifest.json").read_text())
    key = base64.b64decode(manifest["key"], validate=True)
    return "".join(chr(97 + int(n, 16)) for n in hashlib.sha256(key).hexdigest()[:32])


def registration_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library/Application Support/Microsoft Edge/NativeMessagingHosts" / f"{HOST_NAME}.json"


def open_edge(url: str) -> None:
    # Ordinary LaunchServices launch. No profile, cookie or debugging arguments.
    subprocess.run(["/usr/bin/open", "-a", "Microsoft Edge", url], check=True)


def _atomic(path: Path, value: str, mode: int) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("x") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def install(root: Path, *, home: Path | None = None, open_ui: bool = True) -> dict:
    root = root.resolve()
    extension_id = identity(root)
    python = root / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("PROJECT_PYTHON_MISSING")
    user_home = home or Path.home()
    destination = user_home / "Applications/AutoGrab Edge Companion"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = destination / "native-host"
    body = ("#!/bin/sh\n# AutoGrab-owned Native Messaging launcher\nset -eu\numask 077\n"
            f"cd {shlex.quote(str(root))}\n"
            f"exec {shlex.quote(str(python))} -m autograb.edge.native_host --root {shlex.quote(str(root))} "
            f"--extension-id {extension_id} \"$@\"\n")
    target = registration_path(user_home)
    manifest = {"name": HOST_NAME, "description": "AutoGrab Edge Companion local bridge", "path": str(launcher),
                "type": "stdio", "allowed_origins": [f"chrome-extension://{extension_id}/"]}
    # Refuse to take over another application's registration or user launcher.
    if target.exists() and json.loads(target.read_text()) != manifest:
        raise ValueError("EXISTING_HOST_REGISTRATION_CONFLICT")
    if launcher.exists() and launcher.read_text() != body:
        raise ValueError("EXISTING_HOST_LAUNCHER_CONFLICT")
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic(launcher, body, 0o700)
    _atomic(target, json.dumps(manifest, indent=2) + "\n", 0o600)
    if open_ui:
        open_edge("edge://extensions")
        subprocess.run(["/usr/bin/open", str(root / "edge-extension")], check=True)
    return {"host_registered": True, "extension_id": extension_id, "extension_directory": str(root / "edge-extension"),
            "installed": "AWAITING_BROWSER_CONFIRMATION", "live": "OFF"}


def installation_status(root: Path) -> dict:
    try:
        extension_id = identity(root)
        target = registration_path()
        manifest = json.loads(target.read_text()) if target.exists() else {}
        valid = (manifest.get("name") == HOST_NAME and manifest.get("type") == "stdio"
                 and manifest.get("allowed_origins") == [f"chrome-extension://{extension_id}/"]
                 and Path(manifest.get("path", "/missing")).is_file())
        return {"host_registered": valid, "extension_id": extension_id, "installed": "UNKNOWN"}
    except (ValueError, KeyError, OSError):
        return {"host_registered": False, "installed": "UNKNOWN"}
