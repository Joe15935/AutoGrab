"""Public observations and disposable read projections; SQLite stays authoritative."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .models import Product
from .timing import utc_now


@dataclass(frozen=True)
class ProviderSnapshot:
    provider: str
    products: tuple[Product, ...]
    timestamp: str

    @classmethod
    def observed(cls, provider, products):
        if not products or any(p.provider != provider for p in products):
            raise ValueError("Snapshot requires products from exactly one provider")
        return cls(provider, tuple(products), utc_now())

    def ingest(self, store, *, source):
        return store.ingest(list(self.products), source=source)


class ScanState:
    """Core-owned public scan scratch, injected into stateless provider adapters."""
    def __init__(self, store, key):
        self.store, self.key = store, key
        store.connection.execute("""CREATE TABLE IF NOT EXISTS public_scan_state (
            scope TEXT PRIMARY KEY, value_json TEXT NOT NULL, expires_at REAL NOT NULL)""")

    def load(self):
        row = self.store.connection.execute(
            "SELECT value_json,expires_at FROM public_scan_state WHERE scope=?", (self.key,)).fetchone()
        return json.loads(row["value_json"]) if row and row["expires_at"] > time.time() else None

    def save(self, value, *, ttl=86400):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 8_000_000:
            raise ValueError("Scan scratch limit exceeded")
        self.store.connection.execute("""INSERT INTO public_scan_state VALUES(?,?,?)
            ON CONFLICT(scope) DO UPDATE SET value_json=excluded.value_json,
            expires_at=excluded.expires_at""", (self.key, payload, time.time() + ttl))

    def clear(self):
        self.store.connection.execute("DELETE FROM public_scan_state WHERE scope=?", (self.key,))


def _projection_path(root, name):
    from autograb.providers.registry import NAMES
    if name not in NAMES:
        raise ValueError("Unknown provider")
    data = Path(root) / "data"
    if data.is_symlink():
        raise ValueError("Unsafe query directory")
    return data / ("public-snapshot-" + name + ".json")


def save_projection(root, store, name, report):
    """An atomic, rebuildable view allows QUERY to avoid SQLite WAL writes."""
    path = _projection_path(root, name)
    if path.is_symlink():
        raise ValueError("Unsafe query file")
    baseline = store.connection.execute("SELECT * FROM baselines WHERE provider=?", (name,)).fetchone()
    products = [p.to_dict() for p in store.list_products() if p.provider == name]
    document = {"provider": name, "saved_at": utc_now(), "fresh": False,
                "source": "LAST_SAVED_OBSERVATION", "baseline": dict(baseline) if baseline else None,
                "products": products, "last_run": report}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".snapshot-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def query_projection(root, name):
    path = _projection_path(root, name)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"provider": name, "source": "LAST_SAVED_OBSERVATION", "fresh": False,
                "status": "NO_SAVED_OBSERVATION", "baseline": None, "products": []}
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > 16_000_000:
            raise ValueError("Unsafe query file")
        with os.fdopen(descriptor, encoding="utf-8", closefd=False) as stream:
            result = json.load(stream)
        if result.get("provider") != name or result.get("fresh") is not False:
            raise ValueError("Invalid query projection")
        return result
    finally:
        os.close(descriptor)
