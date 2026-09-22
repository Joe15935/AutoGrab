"""Durable, one-shot purchase intents; no network or payment operations.

The commit performed by ``mark_submitting`` MUST precede a provider dispatch.
An uncertain dispatch retains the product lock across restarts. Reconciliation
may discover an order or confirm absence, but never grants another POST. A
confirmed-absent result is deliberately left for explicit operator handling.
Only safe public identifiers belong here; never account, form or session data.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone
import json
import re
import sqlite3
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from autograb.core.models import Product
from autograb.edge.protocol import product_identity
from autograb.storage.database import Store


TERMINAL_STATES = frozenset({"ORDER_EXPIRED", "ORDER_CANCELLED", "PRE_SUBMIT_ABORTED"})
STATES = frozenset({
    "INTENT_CREATED", "CART_READY", "CHECKOUT_READY", "ORDER_SUBMITTING",
    "RECONCILIATION_REQUIRED", "ORDER_SUBMIT_FAILED", "ORDER_CREATED",
    "INVOICE_CREATED", "PAYMENT_URL_READY", "PAYMENT_READY", "WAITING_FOR_USER",
    *TERMINAL_STATES,
})
_RESULT_STATES = frozenset({
    "ORDER_SUBMITTING", "RECONCILIATION_REQUIRED", "ORDER_SUBMIT_FAILED",
    "ORDER_CREATED", "INVOICE_CREATED", "PAYMENT_URL_READY", "PAYMENT_READY",
    "WAITING_FOR_USER",
})
_PRE_SUBMIT = frozenset({"INTENT_CREATED", "CART_READY", "CHECKOUT_READY"})


def _migrate_provider_constraint(store):
    """Rebuild only the legacy CHECK, preserving all added columns and indexes.

    SQLite cannot ALTER a CHECK constraint. Use its documented table-rebuild
    sequence, with foreign-key checking before commit and the original pragma
    restored even after rollback. Never drop or overwrite a migration collision.
    """
    connection = store.connection
    row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='purchase_intents'").fetchone()
    if row is None:
        return
    pattern = r"CHECK\s*\(\s*provider\s*=\s*'bandwagon'\s*\)"
    if not re.search(pattern, row[0], re.I):
        return
    if connection.in_transaction:
        raise ValueError("INTENT_MIGRATION_TRANSACTION_ACTIVE")
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    legacy_alter = connection.execute("PRAGMA legacy_alter_table").fetchone()[0]
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("PRAGMA legacy_alter_table=ON")
    try:
        with store._transaction():
            original = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='purchase_intents'").fetchone()[0]
            if not re.search(pattern, original, re.I):
                return  # Another connection completed the migration before BEGIN.
            declaration = re.sub(pattern, "CHECK(provider IN ('bandwagon','dmit','vmiss','vps','apple'))", original, flags=re.I)
            declaration, count = re.subn(r'(?i)^(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)["`\[]?purchase_intents["`\]]?',
                                        r'\1"purchase_intents_provider_migration"', declaration, count=1)
            if count != 1 or connection.execute("SELECT 1 FROM sqlite_master WHERE name='purchase_intents_provider_migration'").fetchone():
                raise ValueError("INTENT_MIGRATION_SCHEMA_CONFLICT")
            objects = connection.execute("SELECT sql FROM sqlite_master WHERE tbl_name='purchase_intents' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
            # table_xinfo includes generated columns; SQLite recomputes those.
            columns = [r[1] for r in connection.execute("PRAGMA table_xinfo(purchase_intents)") if r[6] == 0]
            names = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            connection.execute(declaration)
            connection.execute(f'INSERT INTO purchase_intents_provider_migration(rowid,{names}) SELECT rowid,{names} FROM purchase_intents')
            connection.execute("DROP TABLE purchase_intents")
            connection.execute("ALTER TABLE purchase_intents_provider_migration RENAME TO purchase_intents")
            for item in objects:
                connection.execute(item[0])
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("INTENT_MIGRATION_FOREIGN_KEY_CHECK_FAILED")
    finally:
        connection.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")
        connection.execute(f"PRAGMA legacy_alter_table={int(legacy_alter)}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _identifier(value: str | None, name: str, *, numeric: bool = False) -> str | None:
    if value is None:
        return None
    pattern = r"[1-9][0-9]{0,39}" if numeric else r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {name}")
    return value


def validate_payment_url(value: str | None, invoice_id: str | None = None) -> str | None:
    """Allow the official invoice link only, without credentials or token query.

    This is URL hygiene, NOT proof that a payment page is valid. The caller must
    independently observe merchant, product, amount, invoice and unpaid status.
    """
    if value is None:
        return None
    if not isinstance(value, str) or any(ord(character) <= 32 for character in value):
        raise ValueError("Invalid payment URL")
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.netloc != "bandwagonhost.com"
            or parts.path != "/viewinvoice.php" or parts.fragment):
        raise ValueError("Invalid payment URL")
    query = parse_qs(parts.query, keep_blank_values=True)
    if set(query) != {"id"} or len(query["id"]) != 1:
        raise ValueError("Invalid payment URL")
    linked_invoice = _identifier(query["id"][0], "invoice ID", numeric=True)
    if invoice_id is not None and linked_invoice != invoice_id:
        raise ValueError("Payment URL invoice mismatch")
    # Canonical spelling rejects encoded keys/values and trailing separators.
    canonical = f"https://bandwagonhost.com/viewinvoice.php?id={linked_invoice}"
    if value != canonical:
        raise ValueError("Invalid payment URL")
    return canonical


class IntentConflict(ValueError):
    """A safe error code plus existing local ID, never private provider text."""

    def __init__(self, code: str, intent_id: str):
        self.code = code
        self.intent_id = intent_id
        super().__init__(code)


class IntentStore:
    """Additive Phase 2 storage using the existing private SQLite connection."""

    def __init__(self, store: Store):
        self.store = store
        self.connection = store.connection
        _migrate_provider_constraint(store)
        # Execute individually: executescript could implicitly commit a caller's
        # transaction. This migration is atomic and leaves Phase 1 tables alone.
        with store._transaction():
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS purchase_intents (
                    intent_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL CHECK(provider IN ('bandwagon','dmit','vmiss','vps','apple')),
                    product_id TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
                    origin TEXT NOT NULL CHECK(origin IN ('REAL','SIMULATED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    cart_identifier TEXT,
                    order_id TEXT,
                    invoice_id TEXT,
                    payment_url TEXT,
                    payment_page_verified INTEGER NOT NULL DEFAULT 0,
                    submit_started_at TEXT,
                    submit_finished_at TEXT,
                    reconciliation_status TEXT,
                    reconciled_at TEXT,
                    verification_json TEXT,
                    terminal_evidence_json TEXT
                )
            """)
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(purchase_intents)")}
            if "terminal_evidence_json" not in columns:
                self.connection.execute("ALTER TABLE purchase_intents ADD COLUMN terminal_evidence_json TEXT")
            self.connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS purchase_intents_active_product
                ON purchase_intents(provider, product_id)
                WHERE state NOT IN ('ORDER_EXPIRED','ORDER_CANCELLED','PRE_SUBMIT_ABORTED')
            """)
            self.connection.execute("""
                CREATE INDEX IF NOT EXISTS purchase_intents_created
                ON purchase_intents(created_at DESC)
            """)

    @staticmethod
    def _intent(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["payment_page_verified"] = bool(value["payment_page_verified"])
        value["verification"] = json.loads(value.pop("verification_json") or "null")
        value["terminal_evidence"] = json.loads(value.pop("terminal_evidence_json") or "null")
        return value

    def get(self, intent_id: str) -> dict[str, Any] | None:
        return self._intent(self.connection.execute(
            "SELECT * FROM purchase_intents WHERE intent_id=?", (intent_id,),
        ).fetchone())

    def _required(self, intent_id: str) -> dict[str, Any]:
        intent = self.get(intent_id)
        if intent is None:
            raise KeyError(intent_id)
        return intent

    def get_by_event(self, event_id: str) -> dict[str, Any] | None:
        return self._intent(self.connection.execute(
            "SELECT * FROM purchase_intents WHERE event_id=?", (event_id,),
        ).fetchone())

    def list(self, *, active_only: bool = False, origin: str | None = None,
             limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or limit < 1 or limit > 10000:
            raise ValueError("Intent limit must be between 1 and 10000")
        if type(active_only) is not bool or origin not in {None, "REAL", "SIMULATED"}:
            raise ValueError("Invalid intent list filter")
        clauses, arguments = [], []
        if active_only:
            clauses.append("state NOT IN ('ORDER_EXPIRED','ORDER_CANCELLED','PRE_SUBMIT_ABORTED')")
        if origin is not None:
            clauses.append("origin=?")
            arguments.append(origin)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [self._intent(row) for row in self.connection.execute(
            f"SELECT * FROM purchase_intents{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (*arguments, limit),
        )]  # type: ignore[misc]

    def active_for_product(self, product_id: str, provider: str = "bandwagon") -> dict[str, Any] | None:
        return self._intent(self.connection.execute("""
            SELECT * FROM purchase_intents WHERE provider=? AND product_id=?
            AND state NOT IN ('ORDER_EXPIRED','ORDER_CANCELLED','PRE_SUBMIT_ABORTED')
        """, (provider, product_id)).fetchone())

    def create(self, event: dict[str, Any], product: Product) -> dict[str, Any]:
        if not isinstance(event, dict) or not isinstance(product, Product):
            raise ValueError("Intent needs a persisted event and product")
        product_identity(product.provider, product.product_id)
        with self.store._transaction():
            saved = self.store.get_event(event.get("id"))
            if saved is None or any(saved[key] != event.get(key) for key in
                                    ("provider", "product_id", "simulated", "event_type")):
                raise ValueError("Event does not match persisted record")
            if saved["provider"] != product.provider or saved["product_id"] != product.product_id:
                raise ValueError("Event product mismatch")
            existing = self.get_by_event(saved["id"])
            if existing is not None:
                raise IntentConflict("INTENT_ALREADY_EXISTS", existing["intent_id"])
            active = self.active_for_product(product.product_id, product.provider)
            if active is not None:
                raise IntentConflict("ACTIVE_INTENT_EXISTS", active["intent_id"])
            now = _now()
            intent_id = str(uuid4())
            self.connection.execute("""
                INSERT INTO purchase_intents(intent_id,provider,product_id,event_id,origin,
                                             created_at,updated_at,state)
                VALUES(?,?,?,?,?,?,?,'INTENT_CREATED')
            """, (intent_id, product.provider, product.product_id, saved["id"],
                  "SIMULATED" if saved["simulated"] else "REAL", now, now))
            return self._required(intent_id)

    def set_cart(self, intent_id: str, cart_identifier: str) -> dict[str, Any]:
        if cart_identifier is None:
            raise ValueError("Cart identifier is required")
        _identifier(cart_identifier, "cart identifier")
        with self.store._transaction():
            current = self._required(intent_id)
            if current["state"] not in {"INTENT_CREATED", "CART_READY"}:
                raise ValueError("Cart cannot change after checkout or dispatch")
            if current["cart_identifier"] not in {None, cart_identifier}:
                raise ValueError("Cart identifier is immutable")
            self.connection.execute("""
                UPDATE purchase_intents SET cart_identifier=?,state='CART_READY',updated_at=?
                WHERE intent_id=?
            """, (cart_identifier, _now(), intent_id))
            return self._required(intent_id)

    def mark_checkout_ready(self, intent_id: str) -> dict[str, Any]:
        with self.store._transaction():
            current = self._required(intent_id)
            if current["state"] not in {"CART_READY", "CHECKOUT_READY"}:
                raise ValueError("Checkout requires a prepared cart")
            self.connection.execute("""
                UPDATE purchase_intents SET state='CHECKOUT_READY',updated_at=? WHERE intent_id=?
            """, (_now(), intent_id))
            return self._required(intent_id)

    def mark_submitting(self, intent_id: str) -> bool:
        """Commit the only dispatch permission. False ALWAYS means no POST."""
        with self.store._transaction():
            current = self._required(intent_id)
            if current["provider"] != "bandwagon":
                return False  # New providers have no order adapter or authority.
            now = _now()
            cursor = self.connection.execute("""
                UPDATE purchase_intents
                SET state='ORDER_SUBMITTING',submit_started_at=?,updated_at=?
                WHERE intent_id=? AND provider='bandwagon' AND state='CHECKOUT_READY' AND submit_started_at IS NULL
            """, (now, now, intent_id))
            return cursor.rowcount == 1

    def mark_uncertain(self, intent_id: str) -> dict[str, Any]:
        with self.store._transaction():
            current = self._required(intent_id)
            if current["state"] not in {"ORDER_SUBMITTING", "RECONCILIATION_REQUIRED"}:
                raise ValueError("Only an unresolved submission can become uncertain")
            self.connection.execute("""
                UPDATE purchase_intents SET state='RECONCILIATION_REQUIRED',updated_at=?
                WHERE intent_id=?
            """, (_now(), intent_id))
            return self._required(intent_id)

    def _persist_result(self, intent_id: str, *, order_id: str | None = None,
                        invoice_id: str | None = None, payment_url: str | None = None,
                        payment_page_verified: bool = False,
                        verification: dict[str, Any] | None = None,
                        allow_pre_submit: bool = False) -> dict[str, Any]:
        current = self._required(intent_id)
        allowed = _RESULT_STATES | (_PRE_SUBMIT if allow_pre_submit else frozenset())
        if current["state"] not in allowed:
            raise ValueError("Order result is not allowed in this state")
        if type(payment_page_verified) is not bool:
            raise ValueError("Payment verification must be explicit boolean evidence")
        supplied = {
            "order_id": _identifier(order_id, "order ID"),
            "invoice_id": _identifier(invoice_id, "invoice ID", numeric=True),
            "payment_url": payment_url,
        }
        merged = {}
        for name, value in supplied.items():
            if current[name] is not None and value is not None and current[name] != value:
                raise ValueError("Known order identifiers cannot be replaced")
            merged[name] = value if value is not None else current[name]
        merged["payment_url"] = validate_payment_url(merged["payment_url"], merged["invoice_id"])
        if merged["order_id"] is None:
            raise ValueError("An observed order ID is required")
        if merged["payment_url"] is not None and merged["invoice_id"] is None:
            raise ValueError("Payment URL requires an observed invoice ID")
        if payment_page_verified and verification is None:
            raise ValueError("Payment verification requires observed page evidence")
        evidence = current["verification"]
        if verification is not None:
            evidence = self._validate_verification(current, merged, verification)
        verified = evidence is not None
        if verified and (merged["invoice_id"] is None or merged["payment_url"] is None):
            raise ValueError("Verified payment page needs order, invoice and URL")
        if current["state"] == "WAITING_FOR_USER" and verified:
            state = "WAITING_FOR_USER"
        elif verified:
            state = "PAYMENT_READY"
        elif merged["payment_url"] is not None:
            state = "PAYMENT_URL_READY"
        elif merged["invoice_id"] is not None:
            state = "INVOICE_CREATED"
        else:
            state = "ORDER_CREATED"
        now = _now()
        self.connection.execute("""
            UPDATE purchase_intents SET order_id=?,invoice_id=?,payment_url=?,
                payment_page_verified=?,state=?,submit_finished_at=COALESCE(submit_finished_at,?),
                verification_json=?,updated_at=? WHERE intent_id=?
        """, (merged["order_id"], merged["invoice_id"], merged["payment_url"],
              int(verified), state, now if current["submit_started_at"] else None,
              json.dumps(evidence, sort_keys=True) if evidence is not None else None, now, intent_id))
        return self._required(intent_id)

    @staticmethod
    def _validate_verification(current: dict[str, Any], result: dict[str, Any],
                               evidence: dict[str, Any]) -> dict[str, Any]:
        flags = {"merchant_verified", "product_verified", "amount_present",
                 "payment_page_verified", "unpaid_verified"}
        required = flags | {"merchant", "product_id", "order_id", "invoice_id",
                            "payment_url", "invoice_status", "source"}
        if not isinstance(evidence, dict) or not required <= evidence.keys():
            raise ValueError("Payment verification evidence is incomplete")
        if evidence.keys() - (required | {"amount", "billing", "deadline"}):
            raise ValueError("Only public payment evidence may be persisted")
        if any(evidence[flag] is not True for flag in flags):
            raise ValueError("Payment page checks must all pass")
        if evidence["merchant"] != "bandwagonhost.com" or evidence["invoice_status"] != "UNPAID":
            raise ValueError("Payment page must belong to the merchant and remain unpaid")
        if evidence["product_id"] != current["product_id"]:
            raise ValueError("Payment page product mismatch")
        if any(result[key] is None or evidence[key] != result[key]
               for key in ("order_id", "invoice_id", "payment_url")):
            raise ValueError("Payment page order or invoice mismatch")
        allowed_sources = {"REAL_SITE"} if current["origin"] == "REAL" else {"MOCK", "SIMULATED"}
        if evidence["source"] not in allowed_sources:
            raise ValueError("Payment evidence origin mismatch")
        amount = evidence.get("amount")
        if amount is not None and (not isinstance(amount, str) or len(amount) > 80
                or not re.fullmatch(r"[A-Z$€£¥₹0-9., +()-]+", amount) or not re.search(r"\d", amount)):
            raise ValueError("Invalid public amount")
        billing = evidence.get("billing")
        if billing is not None and billing not in {
                "Monthly", "Quarterly", "Semi-Annually", "Annually", "Biennially", "Triennially",
                "monthly", "quarterly", "semiannually", "annually", "biennially", "triennially"}:
            raise ValueError("Invalid public billing cycle")
        deadline = evidence.get("deadline")
        if deadline is not None:
            if not isinstance(deadline, str) or len(deadline) > 40:
                raise ValueError("Invalid public payment deadline")
            try:
                datetime.fromisoformat(deadline)
            except ValueError as exc:
                raise ValueError("Invalid public payment deadline") from exc
        return dict(evidence)

    def persist_result(self, intent_id: str, *, order_id: str | None = None,
                       invoice_id: str | None = None, payment_url: str | None = None,
                       payment_page_verified: bool = False,
                       verification: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.store._transaction():
            return self._persist_result(intent_id, order_id=order_id, invoice_id=invoice_id,
                                        payment_url=payment_url,
                                        payment_page_verified=payment_page_verified,
                                        verification=verification)

    def reconcile(self, intent_id: str, status: str, *, order_id: str | None = None,
                  invoice_id: str | None = None, payment_url: str | None = None,
                  payment_page_verified: bool = False,
                  verification: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record provider evidence. ABSENT is not permission to resubmit.

        FOUND can attach a previously existing order before any dispatch.
        UNKNOWN preserves identifiers and never downgrades known order evidence.
        """
        if status not in {"FOUND", "ABSENT", "UNKNOWN"}:
            raise ValueError("Unknown reconciliation status")
        with self.store._transaction():
            current = self._required(intent_id)
            if current["state"] in TERMINAL_STATES:
                raise ValueError("Cannot reconcile a terminal intent")
            if status == "FOUND":
                self._persist_result(intent_id, order_id=order_id, invoice_id=invoice_id,
                                     payment_url=payment_url,
                                     payment_page_verified=payment_page_verified,
                                     verification=verification,
                                     allow_pre_submit=True)
            else:
                if any(value is not None for value in (order_id, invoice_id, payment_url, verification)) or payment_page_verified:
                    raise ValueError("Only FOUND may supply order evidence")
                if status == "ABSENT" and current["order_id"] is not None:
                    raise ValueError("Absence cannot replace an already observed order")
                if current["submit_started_at"] is None:
                    raise ValueError("No dispatched submission to reconcile")
                state = current["state"]
                if current["order_id"] is None:
                    state = "ORDER_SUBMIT_FAILED" if status == "ABSENT" else "RECONCILIATION_REQUIRED"
                self.connection.execute("""
                    UPDATE purchase_intents SET state=?,updated_at=?,
                        submit_finished_at=CASE WHEN ?='ABSENT'
                            THEN COALESCE(submit_finished_at,?) ELSE submit_finished_at END
                    WHERE intent_id=?
                """, (state, _now(), status, _now(), intent_id))
            self.connection.execute("""
                UPDATE purchase_intents SET reconciliation_status=?,reconciled_at=?,updated_at=?
                WHERE intent_id=?
            """, (status, _now(), _now(), intent_id))
            return self._required(intent_id)

    def mark_waiting(self, intent_id: str) -> dict[str, Any]:
        with self.store._transaction():
            current = self._required(intent_id)
            if current["state"] not in {"PAYMENT_READY", "WAITING_FOR_USER"}:
                raise ValueError("Waiting for payment requires a verified payment page")
            self.connection.execute("""
                UPDATE purchase_intents SET state='WAITING_FOR_USER',updated_at=? WHERE intent_id=?
            """, (_now(), intent_id))
            return self._required(intent_id)

    def abort_before_submit(self, intent_id: str) -> dict[str, Any]:
        # A broker cancellation joins its transaction so the cancellation proof,
        # terminal record and executor release commit together.
        with nullcontext() if self.connection.in_transaction else self.store._transaction():
            current = self._required(intent_id)
            if current["state"] not in _PRE_SUBMIT or current["submit_started_at"] is not None:
                raise ValueError("A dispatched intent cannot be discarded")
            self.connection.execute("""
                UPDATE purchase_intents SET state='PRE_SUBMIT_ABORTED',updated_at=? WHERE intent_id=?
            """, (_now(), intent_id))
            return self._required(intent_id)

    def record_terminal(self, intent_id: str, state: str, *, server_confirmed: bool = False,
                        terminal_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        """Release an active-product lock only for the matching observed order.

        This records a server observation; it never sends a cancellation. A
        boolean confirmation alone cannot bind that observation to this order.
        """
        if state not in {"ORDER_EXPIRED", "ORDER_CANCELLED"} or server_confirmed is not True:
            raise ValueError("Terminal order status requires server evidence")
        with self.store._transaction():
            current = self._required(intent_id)
            if current["order_id"] is None or current["state"] in TERMINAL_STATES - {state}:
                raise ValueError("Terminal order must be an existing matching order")
            required = {"provider", "product_id", "order_id", "invoice_id", "source", "status"}
            if not isinstance(terminal_evidence, dict) or terminal_evidence.keys() != required:
                raise ValueError("Terminal order status requires complete public evidence")
            if any(terminal_evidence[key] != current[key]
                   for key in ("provider", "product_id", "order_id", "invoice_id")):
                raise ValueError("Terminal order evidence identity mismatch")
            allowed_sources = {"REAL_SITE"} if current["origin"] == "REAL" else {"MOCK", "SIMULATED"}
            if terminal_evidence["source"] not in allowed_sources or terminal_evidence["status"] != state:
                raise ValueError("Terminal order evidence source or status mismatch")
            self.connection.execute("""
                UPDATE purchase_intents SET state=?,payment_page_verified=0,
                    terminal_evidence_json=?,updated_at=? WHERE intent_id=?
            """, (state, json.dumps(terminal_evidence, sort_keys=True), _now(), intent_id))
            return self._required(intent_id)

    def recover_interrupted(self) -> list[dict[str, Any]]:
        """Under the application's process lock, preserve possible server commits."""
        with self.store._transaction():
            rows = self.connection.execute(
                "SELECT intent_id FROM purchase_intents WHERE state='ORDER_SUBMITTING'"
            ).fetchall()
            self.connection.execute("""
                UPDATE purchase_intents SET state='RECONCILIATION_REQUIRED',updated_at=?
                WHERE state='ORDER_SUBMITTING'
            """, (_now(),))
            return [self._required(row["intent_id"]) for row in rows]

    def recovery_intents(self) -> list[dict[str, Any]]:
        """Unresolved dispatches and orders still missing a verified payment page."""
        return [self._intent(row) for row in self.connection.execute("""
            SELECT * FROM purchase_intents
            WHERE state IN ('ORDER_SUBMITTING','RECONCILIATION_REQUIRED',
                            'ORDER_CREATED','INVOICE_CREATED','PAYMENT_URL_READY')
            ORDER BY created_at
        """)]  # type: ignore[misc]
