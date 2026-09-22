"""Transactional SQLite state for catalogue baselines and one-shot event work.

``ingest`` receives a complete parsed catalogue, never a partially parsed page.
Callers must pass ``complete=False`` when completeness cannot be established.
Unknown availability never replaces a previously observed stock state. Events
remain separate from product state, including when created for a simulation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterator
from uuid import uuid4

from autograb.core.models import Product
from autograb.core.opportunity import classify_event, stable


_AVAILABILITIES = {"AVAILABLE", "SOLD_OUT", "UNKNOWN"}
_EVENT_TYPES = {"NEW_PRODUCT", "RESTOCK", "PRODUCT_CHANGED", "PROMOTIONAL_EVENT", "SIMULATED"}
_IN_FLIGHT_EVENT_STATUSES = (
    "RUNNING", "DETECTED", "VERIFYING", "PRODUCT_VERIFIED", "OPENING_BROWSER",
    "CART_READY", "DRY_RUN_BOUNDARY_REACHED", "NOTIFYING",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _check_local_file(path: Path) -> None:
    """Reject redirected/shared paths without opening or changing their target."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("SQLite files must be ordinary files without links")


def _make_private(path: Path, *, create: bool = False) -> None:
    if create:
        # Do not open/close an existing SQLite file independently: closing an
        # unrelated descriptor can release this process's POSIX SQLite locks.
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    _check_local_file(path)
    try:
        os.chmod(path, 0o600, follow_symlinks=False)
    except FileNotFoundError:
        if create:
            raise
        # SQLite can remove an unused sidecar when a connection closes.


def _validate_product(product: Product) -> None:
    if not isinstance(product, Product):
        raise ValueError("Every catalogue entry must be a Product")
    for name in ("provider", "product_id", "name", "product_url"):
        value = getattr(product, name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Product {name} must be a nonempty string")
    if not isinstance(product.availability, str) or product.availability not in _AVAILABILITIES:
        raise ValueError("Product availability must be AVAILABLE, SOLD_OUT or UNKNOWN")
    if not isinstance(product.eligible, bool):
        raise ValueError("Product eligible must be boolean")
    if not isinstance(product.prices, list) or not all(isinstance(price, dict) for price in product.prices):
        raise ValueError("Product prices must be a list of objects")
    for name in ("categories", "locations"):
        value = getattr(product, name)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Product {name} must be a list of strings")
    if product.order_url is not None and not isinstance(product.order_url, str):
        raise ValueError("Product order_url must be a string or None")
    if not isinstance(product.metadata, dict):
        raise ValueError("Product metadata must be an object")
    try:
        _json(product.to_dict())
    except (TypeError, ValueError) as exc:
        raise ValueError("Product must contain finite JSON-compatible values") from exc


class Store:
    """A connection scoped to its calling thread; separate processes are safe.

    Writes use BEGIN IMMEDIATE and durable SQLite commits. ``update_event``
    merges details into the existing object, with new keys winning. Callers
    must provide only non-sensitive details; no browser state belongs here.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        local_files = []
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Check every possible SQLite sidecar before changing even the DB's
            # permissions. The rollback journal can exist before WAL is enabled.
            local_files = [Path(self.path + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
            for local_file in local_files:
                _check_local_file(local_file)
            for index, local_file in enumerate(local_files):
                _make_private(local_file, create=index == 0)
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS baselines (
                provider TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL,
                last_scan_at TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products (
                provider TEXT NOT NULL,
                product_id TEXT NOT NULL,
                product_json TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (provider, product_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                product_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                generation INTEGER NOT NULL,
                product_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                simulated INTEGER NOT NULL,
                dedupe_key TEXT UNIQUE,
                details_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (provider, product_id, event_type, generation, simulated)
            );
            CREATE INDEX IF NOT EXISTS events_created ON events(created_at DESC);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL REFERENCES events(id),
                status TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        for local_file in local_files:
            _make_private(local_file)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    @staticmethod
    def _event(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["product"] = json.loads(result.pop("product_json"))
        result["details"] = json.loads(result.pop("details_json"))
        result["simulated"] = bool(result["simulated"])
        return result

    def ingest(self, products: list[Product], source: str = "catalogue", *, complete: bool = True) -> dict[str, Any]:
        """Commit one full scan or raise without writing any partial changes.

        The first valid scan initializes a provider baseline with no events.
        A missing product is retained. UNKNOWN stock observations preserve the
        last known availability, so SOLD_OUT -> UNKNOWN -> AVAILABLE is still
        one confirmed RESTOCK, whereas AVAILABLE -> UNKNOWN -> AVAILABLE is not.
        Availability becoming SOLD_OUT updates state but is not a trigger.
        PRODUCT_CHANGED is reserved for changed product metadata.
        """
        if complete is not True:
            raise ValueError("Cannot ingest an incomplete catalogue")
        if not isinstance(products, list) or not products:
            raise ValueError("Cannot ingest an empty catalogue")
        seen = set()
        for product in products:
            _validate_product(product)
            identity = (product.provider, product.product_id)
            if identity in seen:
                raise ValueError("Duplicate product identity in catalogue")
            seen.add(identity)
        providers = {product.provider for product in products}
        if len(providers) != 1:
            raise ValueError("A catalogue scan must contain exactly one provider")
        provider = next(iter(providers))
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Catalogue source must be a nonempty string")
        now = _now()
        events: list[dict[str, Any]] = []
        with self._transaction():
            baseline = self.connection.execute("SELECT 1 FROM baselines WHERE provider=?", (provider,)).fetchone()
            initialized = baseline is None
            known = [json.loads(row[0]) for row in self.connection.execute(
                "SELECT product_json FROM products WHERE provider=?", (provider,))]
            known_groups = {group for item in known for group in item.get("categories", [])}
            known_urls = {item.get("order_url") for item in known if item.get("order_url")}
            for product in products:
                row = self.connection.execute(
                    "SELECT product_json, revision FROM products WHERE provider=? AND product_id=?",
                    (provider, product.product_id),
                ).fetchone()
                incoming = product.to_dict()
                observed = product.to_dict()
                previous = None
                event_type = None
                revision = 0 if row is None else row["revision"]
                if row is None:
                    if not initialized:
                        event_type = "NEW_PRODUCT"
                        revision = 1
                else:
                    previous = json.loads(row["product_json"])
                    if incoming["availability"] == "UNKNOWN":
                        incoming["availability"] = previous["availability"]
                    # Preserve stock history through unknown or stale per-store observations.
                    old_inventory = { (p.get("sku"), p.get("region"), p.get("mode"), p.get("store_id")): p
                                      for p in previous.get("metadata", {}).get("inventory", []) if isinstance(p, dict) }
                    for item in incoming.get("metadata", {}).get("inventory", []):
                        if not isinstance(item, dict):
                            continue
                        old = old_inventory.get((item.get("sku"), item.get("region"), item.get("mode"), item.get("store_id")))
                        if old and (item.get("availability") == "UNKNOWN" or not item.get("fresh", True)):
                            item["availability"] = old.get("availability", "UNKNOWN")
                    if stable(incoming) != stable(previous):
                        revision += 1
                        apple_restock = provider == "apple" and any(kind in {"PICKUP_AVAILABLE", "DELIVERY_AVAILABLE"}
                            for kind in classify_event(previous, observed)["opportunities"])
                        if apple_restock or (provider != "apple" and previous["availability"] == "SOLD_OUT" and incoming["availability"] == "AVAILABLE"):
                            event_type = "RESTOCK"
                        elif {key: value for key, value in stable(incoming).items() if key != "availability"} != {
                            key: value for key, value in stable(previous).items() if key != "availability"
                        }:
                            event_type = "PRODUCT_CHANGED"
                self.connection.execute(
                    """INSERT INTO products(provider,product_id,product_json,revision,first_seen_at,last_seen_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(provider,product_id) DO UPDATE SET
                       product_json=excluded.product_json,revision=excluded.revision,last_seen_at=excluded.last_seen_at""",
                    (provider, product.product_id, _json(incoming), revision, now, now),
                )
                if event_type and not initialized:
                    event = self._create_event(
                        Product.from_dict(incoming), event_type, False,
                        _json(["catalogue", provider, product.product_id, revision, event_type]),
                    )
                    details = classify_event(previous, observed,
                        new_groups=set(product.categories) - known_groups,
                        new_url=bool(product.order_url and product.order_url not in known_urls))
                    self.connection.execute("UPDATE events SET details_json=? WHERE id=?", (_json(details), event["id"]))
                    event["details"] = details
                    events.append(event)
            self.connection.execute(
                """INSERT INTO baselines(provider,initialized_at,last_scan_at,source) VALUES(?,?,?,?)
                   ON CONFLICT(provider) DO UPDATE SET last_scan_at=excluded.last_scan_at,source=excluded.source""",
                (provider, now, now, source),
            )
            count = self.connection.execute("SELECT COUNT(*) FROM products WHERE provider=?", (provider,)).fetchone()[0]
        return {"baseline_initialized": initialized, "known_count": count, "events": events}

    def _create_event(self, product: Product, event_type: str, simulated: bool, dedupe_key: str | None) -> dict[str, Any]:
        if dedupe_key is not None:
            existing = self.connection.execute("SELECT * FROM events WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if existing is not None:
                if (existing["provider"], existing["product_id"], existing["event_type"], bool(existing["simulated"])) != (
                    product.provider, product.product_id, event_type, simulated
                ):
                    raise ValueError("Event dedupe key belongs to another event identity")
                return self._event(existing)  # type: ignore[return-value]
        generation = self.connection.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM events WHERE provider=? AND product_id=? AND event_type=? AND simulated=?",
            (product.provider, product.product_id, event_type, int(simulated)),
        ).fetchone()[0]
        event_id = str(uuid4())
        now = _now()
        self.connection.execute(
            """INSERT INTO events(id,provider,product_id,event_type,generation,product_json,status,created_at,updated_at,simulated,dedupe_key)
               VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)""",
            (event_id, product.provider, product.product_id, event_type, generation, _json(product.to_dict()), now, now, int(simulated), dedupe_key),
        )
        return self.get_event(event_id)  # type: ignore[return-value]

    def create_event(self, product: Product, event_type: str, simulated: bool = False, dedupe_key: str | None = None) -> dict[str, Any]:
        """Create event work without modifying the real catalogue baseline."""
        _validate_product(product)
        if not isinstance(event_type, str) or event_type not in _EVENT_TYPES:
            raise ValueError("Unknown event type")
        if not isinstance(simulated, bool):
            raise ValueError("simulated must be boolean")
        if event_type == "SIMULATED" and not simulated:
            raise ValueError("SIMULATED events must set simulated=True")
        if dedupe_key is not None and (not isinstance(dedupe_key, str) or not dedupe_key):
            raise ValueError("dedupe_key must be a nonempty string or None")
        with self._transaction():
            return self._create_event(product, event_type, simulated, dedupe_key)

    def claim_event(self, event_id: str) -> bool:
        """Atomically consume the single permission to start an event."""
        with self._transaction():
            cursor = self.connection.execute(
                "UPDATE events SET status='RUNNING',updated_at=? WHERE id=? AND status='PENDING'",
                (_now(), event_id),
            )
            return cursor.rowcount == 1

    def update_event(self, event_id: str, status: str, details: dict[str, Any] | None = None) -> None:
        """Merge safe details; an event cannot be reset for another dispatch."""
        if not isinstance(status, str) or not status.strip():
            raise ValueError("Event status must be a nonempty string")
        if status in {"PENDING", "RUNNING"}:
            raise ValueError("Use claim_event to start work; events cannot be requeued")
        if details is not None and not isinstance(details, dict):
            raise ValueError("Event details must be an object")
        with self._transaction():
            row = self.connection.execute("SELECT details_json FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise KeyError(event_id)
            merged = json.loads(row["details_json"])
            merged.update(details or {})
            self.connection.execute("UPDATE events SET status=?,details_json=?,updated_at=? WHERE id=?", (status, _json(merged), _now(), event_id))

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return self._event(self.connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def list_events(self, limit: int = 20) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("Event limit must be a positive integer")
        return [self._event(row) for row in self.connection.execute(
            "SELECT * FROM events ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,),
        )]  # type: ignore[misc]

    def list_products(self) -> list[Product]:
        return [Product.from_dict(json.loads(row["product_json"])) for row in self.connection.execute(
            "SELECT product_json FROM products ORDER BY provider,product_id"
        )]

    def summary(self) -> dict[str, Any]:
        return {
            "baseline_initialized": bool(self.connection.execute("SELECT COUNT(*) FROM baselines").fetchone()[0]),
            "known_count": self.connection.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "event_count": self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "event_statuses": {row["status"]: row["count"] for row in self.connection.execute(
                "SELECT status,COUNT(*) AS count FROM events GROUP BY status"
            )},
            "notification_count": self.connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
            "run_count": self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "baselines": [dict(row) for row in self.connection.execute("SELECT * FROM baselines ORDER BY provider")],
        }

    def start_run(self, kind: str) -> str:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("Run kind must be a nonempty string")
        run_id = str(uuid4())
        with self._transaction():
            self.connection.execute("INSERT INTO runs(id,kind,status,started_at) VALUES(?,?,'RUNNING',?)", (run_id, kind, _now()))
        return run_id

    def finish_run(self, run_id: str, status: str, details: dict[str, Any] | None = None) -> None:
        if not isinstance(status, str) or not status.strip() or status == "RUNNING":
            raise ValueError("A completed run needs a non-running status")
        if details is not None and not isinstance(details, dict):
            raise ValueError("Run details must be an object")
        with self._transaction():
            cursor = self.connection.execute("UPDATE runs SET status=?,finished_at=?,details_json=? WHERE id=?", (status, _now(), _json(details or {}), run_id))
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def record_notification(self, event_id: str, status: str, detail: str = "") -> str:
        if not isinstance(status, str) or not status.strip() or not isinstance(detail, str):
            raise ValueError("Notification needs a nonempty status and string detail")
        notification_id = str(uuid4())
        with self._transaction():
            self.connection.execute("INSERT INTO notifications(id,event_id,status,detail,created_at) VALUES(?,?,?,?,?)", (notification_id, event_id, status, detail, _now()))
        return notification_id

    def recover_interrupted(self) -> int:
        """Mark interrupted work for inspection; never put it back in the queue.

        Call only while holding the application's exclusive process lock, so
        another live worker cannot be mistaken for an interrupted process.
        """
        now = _now()
        with self._transaction():
            placeholders = ",".join("?" for _ in _IN_FLIGHT_EVENT_STATUSES)
            cursor = self.connection.execute(
                f"UPDATE events SET status='INTERRUPTED',updated_at=? WHERE status IN ({placeholders})",
                (now, *_IN_FLIGHT_EVENT_STATUSES),
            )
            count = cursor.rowcount
            self.connection.execute("UPDATE runs SET status='INTERRUPTED',finished_at=? WHERE status='RUNNING'", (now,))
        return count
