"""Small durable HTTP budgets in the existing private product database.

Retry-After follows RFC 9110 section 10.2.3. No response bodies, URLs,
identities, cookies or raw headers are stored here.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import math
import re
import time
import uuid

from autograb.core.errors import AutoGrabError
from autograb.storage.database import Store


def retry_after_seconds(value, now):
    """Normalize delay-seconds or an HTTP-date; malformed values are ignored."""
    if (not isinstance(value, str) or not value.isascii() or len(value) > 128
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        # Large valid delays remain a block; never truncate a server's minimum.
        return float(int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return max(0.0, date.timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return None


class BudgetWait(AutoGrabError):
    def __init__(self, status):
        super().__init__("RATE_LIMIT_WAIT")
        self.budget_status = status


class PublicHTTPError(AutoGrabError):
    """The header is transient and never included in exception text or logs."""
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.retry_after = retry_after


@dataclass(frozen=True)
class RateTicket:
    provider: str
    region: str
    endpoint: str
    nonce: str
    interval: float
    cooldown: float
    probe: bool


class ProviderRateBudget:
    def __init__(self, store, *, clock=None):
        self.path = store.path
        self._memory_store = store if self.path == ":memory:" else None
        self.clock = clock or time.time
        self._ensure_schema(store.connection)

    @staticmethod
    def _ensure_schema(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS provider_rate_budgets (
            provider TEXT NOT NULL, region TEXT NOT NULL, endpoint TEXT NOT NULL,
            blocked_until REAL NOT NULL DEFAULT 0, retry_after REAL,
            consecutive_limits INTEGER NOT NULL DEFAULT 0,
            last_success REAL, last_failure REAL, last_error TEXT,
            next_allowed REAL NOT NULL DEFAULT 0,
            claim_id TEXT, claim_until REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (provider, region, endpoint)
        )""")

    @classmethod
    def record_browser_block(cls, store, provider, region, endpoint, code):
        """Join the caller's correlated-event transaction; never open another DB.

        Browser DOM evidence has no Retry-After header. Preserve any longer
        known deadline and revoke an in-flight HTTP ticket before it can succeed.
        """
        scope = cls._scope(provider, region, endpoint)
        if not store.connection.in_transaction:
            raise ValueError("Browser block requires an active transaction")
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            raise ValueError("Invalid rate error code")
        db, current = store.connection, time.time()
        cls._ensure_schema(db)
        db.execute("INSERT OR IGNORE INTO provider_rate_budgets(provider,region,endpoint) VALUES(?,?,?)", scope)
        row = db.execute("SELECT consecutive_limits FROM provider_rate_budgets WHERE provider=? AND region=? AND endpoint=?", scope).fetchone()
        consecutive = row["consecutive_limits"] + 1
        delay = min(86400, 900 * (2 ** min(consecutive - 1, 7)))
        db.execute("""UPDATE provider_rate_budgets SET blocked_until=MAX(blocked_until,?),
            retry_after=NULL,consecutive_limits=?,last_failure=?,last_error=?,claim_id=NULL,claim_until=0
            WHERE provider=? AND region=? AND endpoint=?""", (current + delay, consecutive, current, code, *scope))

    @contextmanager
    def _store(self):
        # Never share sqlite3's thread-affine connection with HTTP worker threads.
        # Store also rechecks DB/sidecar permissions and symlink boundaries.
        if self._memory_store is not None:
            yield self._memory_store
        else:
            with Store(self.path) as store:
                yield store

    @staticmethod
    def _scope(provider, region, endpoint):
        scope = provider, region, endpoint
        if any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", v) for v in scope):
            raise ValueError("Invalid rate scope")
        return scope

    @staticmethod
    def _public(row, now):
        return {k: row[k] for k in ("provider", "region", "endpoint", "blocked_until", "retry_after",
            "consecutive_limits", "last_success", "last_failure", "last_error", "next_allowed")} | {
            "in_flight": bool(row["claim_until"] > now),
            "wait_seconds": max(0, max(row["blocked_until"], row["next_allowed"], row["claim_until"]) - now)}

    def status(self, provider, region, endpoint):
        scope = self._scope(provider, region, endpoint)
        with self._store() as store:
            row = store.connection.execute("SELECT * FROM provider_rate_budgets WHERE provider=? AND region=? AND endpoint=?", scope).fetchone()
        return self._public(row, self.clock()) if row else {"provider":provider, "region":region, "endpoint":endpoint,
            "blocked_until":0, "retry_after":None, "consecutive_limits":0, "last_success":None,
            "last_failure":None, "last_error":None, "next_allowed":0, "in_flight":False, "wait_seconds":0}

    def claim(self, provider, region, endpoint, *, interval_seconds, cooldown_seconds=900, lease_seconds=90):
        scope = self._scope(provider, region, endpoint)
        values = interval_seconds, cooldown_seconds, lease_seconds
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 1 <= v <= 86400 for v in values):
            raise ValueError("Invalid rate duration")
        now, nonce, denied = self.clock(), str(uuid.uuid4()), None
        with self._store() as store, store._transaction():
            db = store.connection
            db.execute("INSERT OR IGNORE INTO provider_rate_budgets(provider,region,endpoint) VALUES(?,?,?)", scope)
            row = db.execute("SELECT * FROM provider_rate_budgets WHERE provider=? AND region=? AND endpoint=?", scope).fetchone()
            if row["claim_id"] is not None and 0 < row["claim_until"] <= now:
                # A crashed/unknown request is not immediate permission to retry.
                db.execute("""UPDATE provider_rate_budgets SET claim_id=NULL,claim_until=0,
                    blocked_until=MAX(blocked_until,?),last_failure=?,last_error='PROBE_INTERRUPTED'
                    WHERE provider=? AND region=? AND endpoint=?""", (now + cooldown_seconds, now, *scope))
                row = db.execute("SELECT * FROM provider_rate_budgets WHERE provider=? AND region=? AND endpoint=?", scope).fetchone()
            if now < max(row["blocked_until"], row["next_allowed"], row["claim_until"]):
                denied = self._public(row, now)
            else:
                ticket = RateTicket(*scope, nonce, float(interval_seconds), float(cooldown_seconds), bool(row["blocked_until"]))
                db.execute("""UPDATE provider_rate_budgets SET claim_id=?,claim_until=?,next_allowed=?
                    WHERE provider=? AND region=? AND endpoint=?""", (nonce, now + lease_seconds, now + interval_seconds, *scope))
        if denied is not None:
            raise BudgetWait(denied)
        return ticket

    def success(self, ticket):
        return self._finish(ticket, None, None, False)

    def failure(self, ticket, code, *, retry_after=None, limited=False):
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            raise ValueError("Invalid rate error code")
        return self._finish(ticket, code, retry_after, limited)

    def _finish(self, ticket, code, header, limited):
        if not isinstance(ticket, RateTicket):
            raise ValueError("Invalid rate ticket")
        now = self.clock()
        scope = self._scope(ticket.provider, ticket.region, ticket.endpoint)
        with self._store() as store, store._transaction():
            db = store.connection
            row = db.execute("SELECT * FROM provider_rate_budgets WHERE provider=? AND region=? AND endpoint=?", scope).fetchone()
            if (not row or row["claim_id"] != ticket.nonce or
                    (row["claim_until"] <= now and not (code is not None and row["claim_until"] == 0))):
                return False
            if code is None:
                # Retain the nonce until the next claim so a caller's immediate
                # schema validation can downgrade this same HTTP response.
                db.execute("""UPDATE provider_rate_budgets SET claim_until=0,
                    blocked_until=0,retry_after=NULL,consecutive_limits=0,last_success=?,last_error=NULL
                    WHERE provider=? AND region=? AND endpoint=?""", (now, *scope))
            else:
                consecutive = row["consecutive_limits"] + int(limited)
                retry = retry_after_seconds(header, now)
                fallback = min(86400, ticket.cooldown * (2 ** min(max(0, consecutive - 1), 7))) if limited else max(60, ticket.interval)
                delay = max(1, fallback, retry or 0)
                db.execute("""UPDATE provider_rate_budgets SET claim_id=NULL,claim_until=0,
                    blocked_until=?,retry_after=?,consecutive_limits=?,last_failure=?,last_error=?
                    WHERE provider=? AND region=? AND endpoint=?""", (now + delay, retry, consecutive, now, code, *scope))
        return True
