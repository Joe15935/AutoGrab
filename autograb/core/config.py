from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import tomllib


@dataclass
class Config:
    root: Path
    mode: str = "DRY_RUN"
    normal_interval: float = 30
    timeout_ms: int = 20000
    smtp: dict = field(default_factory=dict)
    bandwagon: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path, path: Path | None = None):
        path = path or root / "config/config.toml"
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        if set(data) - {"mode", "normal_interval", "timeout_ms", "smtp", "bandwagon", "providers"}:
            raise ValueError("Unknown configuration keys")
        conf = cls(root=root, **data)
        if conf.mode != "DRY_RUN":
            raise ValueError("Only DRY_RUN is supported")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in (conf.normal_interval, conf.timeout_ms)):
            raise ValueError("Monitoring interval and timeout must be finite numbers")
        if not 30 <= conf.normal_interval <= 3600 or not 5000 <= conf.timeout_ms <= 60000:
            raise ValueError("Use normal_interval 30..3600 seconds and timeout_ms 5000..60000")
        if not isinstance(conf.smtp, dict):
            raise ValueError("SMTP configuration must be a table")
        if (not isinstance(conf.providers, dict)
                or set(conf.providers) - {"bandwagon", "dmit", "vmiss", "vps", "apple"}
                or any(not isinstance(v, dict) for v in conf.providers.values())):
            raise ValueError("Invalid provider settings")
        apple_path = root / "config/apple.local.toml"
        if apple_path.is_symlink() or (apple_path.exists() and (not apple_path.is_file() or apple_path.stat().st_nlink != 1)):
            raise ValueError("Apple local settings must be an ordinary file")
        if apple_path.exists():
            local = tomllib.loads(apple_path.read_text())
            if set(local) != {"apple"} or not isinstance(local["apple"], dict):
                raise ValueError("Invalid Apple local settings")
            conf.providers["apple"] = {**conf.providers.get("apple", {}), **local["apple"]}
        for settings in conf.providers.values():
            if "enabled" in settings and type(settings["enabled"]) is not bool:
                raise ValueError("Provider enabled must be boolean")
        smtp_fields = {"host", "port", "username", "sender", "recipient", "tls_mode",
                       "secret_service", "secret_account", "timeout"}
        if set(conf.smtp) - smtp_fields:
            raise ValueError("Unknown SMTP field; file-based SMTP secrets are not supported")
        if not isinstance(conf.bandwagon, dict) or set(conf.bandwagon) - {"preferred_payment_gateway"}:
            raise ValueError("Unknown Bandwagon setting")
        gateway = conf.bandwagon.get("preferred_payment_gateway")
        if gateway is not None and (not isinstance(gateway, str) or not gateway or len(gateway) > 80
                                    or not all(c.isascii() and (c.isalnum() or c in "_-") for c in gateway)):
            raise ValueError("Invalid gateway identifier")
        return conf

    def prepare(self):
        root = self.root.resolve()
        paths = [root / name for name in ("data", "logs", "artifacts", "profiles", "profiles/bandwagon")]
        # Check all managed paths before touching any existing directory. An
        # explicitly chosen root may resolve through a symlink; managed children
        # must remain private directories inside that resolved root.
        for path in paths:
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError("Managed runtime paths must be ordinary directories")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in paths:
            path.mkdir(exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fchmod(descriptor, 0o700)
            finally:
                os.close(descriptor)
