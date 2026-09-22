"""Append structured, explicitly selected public fields; never raw exceptions."""

import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import parse_qsl, urlsplit
from autograb.core.timing import utc_now


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return normalized in {
        "password", "passwd", "cookie", "cookies", "authorization", "proxyauthorization",
        "token", "session", "csrf", "creditcard", "cardnumber", "cvv", "cvc",
        "privatekey", "secret", "smtpconfig",
    } or normalized.endswith(("password", "token", "secret", "privatekey"))


def _check_public(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or _sensitive_key(key):
                raise ValueError("Sensitive or invalid structured log field")
            _check_public(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _check_public(child)
    elif isinstance(value, str) and value.lower().startswith(("https://", "http://")):
        try:
            url = urlsplit(value)
            if url.username is not None or url.password is not None or any(_sensitive_key(key) for key, _ in parse_qsl(url.query)):
                raise ValueError("Sensitive URL must not enter structured logs")
        except ValueError:
            raise ValueError("Invalid or sensitive URL in structured logs") from None


class EventLog:
    def __init__(self, path: Path, *, mode: str = "DRY_RUN", provider: str = "bandwagon"):
        if mode not in {"DRY_RUN", "LIVE"}:
            raise ValueError("Unknown execution mode")
        self.path = path
        self.mode = mode
        if provider not in {"bandwagon", "dmit", "vmiss", "vps", "apple"}:
            raise ValueError("Unknown provider")
        self.provider = provider

    def write(self, event: str, **fields):
        if {"timestamp", "provider", "mode"} & fields.keys():
            raise ValueError("Structured log context cannot be overridden")
        _check_public(fields)
        record = {"timestamp": utc_now(), "provider": self.provider, "mode": self.mode, "event": event, **fields}
        serialized = json.dumps(record, ensure_ascii=False, allow_nan=False)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Structured log must be an ordinary, unlinked file")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8", closefd=False) as stream:
                stream.write(serialized + "\n")
        finally:
            os.close(descriptor)
        print(serialized, flush=True)
