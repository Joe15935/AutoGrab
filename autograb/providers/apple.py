"""Configured Apple inventory, using public HTTP only; checkout belongs to Edge.

No user target is selected automatically. The pickup JSON contract was verified
against Apple's public store on 2026-09-22. Delivery and order-open signals are
not inferred from pickup availability. No upstream implementation is bundled.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from autograb.core.rate_budget import BudgetWait


REGIONS = {
    "cn": "https://www.apple.com.cn", "us": "https://www.apple.com",
    "hk": "https://www.apple.com/hk-zh", "tw": "https://www.apple.com/tw",
    "jp": "https://www.apple.com/jp", "sg": "https://www.apple.com/sg",
    "au": "https://www.apple.com/au", "my": "https://www.apple.com/my",
}
_SKU = re.compile(r"[A-Z0-9]{5,24}/[A-Z0-9]{1,3}\Z")
_STORE = re.compile(r"R[0-9]{3,5}\Z")
MAX_RESPONSE_BYTES = 2_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _http_get(url):
    """No cookie jar, credentials, browser impersonation, or redirect following."""
    request = Request(url, headers={"Accept": "application/json",
        "User-Agent": "AutoGrab/0.4 (public inventory monitor)"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=20) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                return response.status, None, None
            return response.status, json.loads(data), response.headers.get("Retry-After")
    except HTTPError as error:
        return error.code, None, error.headers.get("Retry-After")
    except (URLError, OSError, ValueError):
        return None, None, None


def _product_url(region, value):
    if not isinstance(value, str) or len(value) > 400 or not value.isascii():
        return None
    base = REGIONS[region]
    try:
        parsed, expected = urlsplit(value), urlsplit(base)
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.netloc != expected.netloc or
            parsed.query or parsed.fragment or "/../" in parsed.path or
            not parsed.path.startswith(expected.path + "/shop/buy-") or
            not re.fullmatch(r"/[a-zA-Z0-9/_-]+", parsed.path)):
        return None
    return value


@dataclass(frozen=True)
class PickupObservation:
    sku: str
    store_id: str
    availability: str = "UNKNOWN"
    status: str = "DATA_UNVERIFIED"
    name: str = ""


def parse_pickup(payload, sku, store_id):
    """Accept only exact requested SKU/store and explicit stock enum values."""
    unknown = PickupObservation(sku, store_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("head"), dict) or str(payload["head"].get("status")) != "200":
        return unknown
    body = payload.get("body")
    if not isinstance(body, dict) or body.get("errorMessage"):
        return unknown
    stores = body.get("stores")
    if stores is None:
        content = body.get("content", {})
        pickup = content.get("pickupMessage", {}) if isinstance(content, dict) else {}
        stores = pickup.get("stores") if isinstance(pickup, dict) else None
    if not isinstance(stores, list):
        return unknown
    matches = [s for s in stores if isinstance(s, dict) and s.get("storeNumber") == store_id]
    if len(matches) != 1:
        return unknown
    parts = matches[0].get("partsAvailability")
    part = parts.get(sku) if isinstance(parts, dict) else None
    if not isinstance(part, dict) or part.get("partNumber", sku) != sku:
        return unknown
    display = part.get("pickupDisplay")
    availability = {"available": "AVAILABLE", "unavailable": "SOLD_OUT"}.get(display) if isinstance(display, str) else None
    if availability is None:
        return PickupObservation(sku, store_id, status="PICKUP_INELIGIBLE" if part.get("pickupDisplay") == "ineligible" else "DATA_UNVERIFIED")
    if availability == "AVAILABLE" and part.get("storePickEligible") is False:
        return unknown
    messages = part.get("messageTypes", {})
    regular = messages.get("regular", {}) if isinstance(messages, dict) else {}
    name = regular.get("storePickupProductTitle", "") if isinstance(regular, dict) else ""
    if not isinstance(name, str) or len(name) > 240 or "<" in name or any(ord(c) < 32 for c in name):
        name = ""
    return PickupObservation(sku, store_id, availability, "OBSERVED", name)


class AppleInventoryMonitor:
    """One bounded public request per interval, rotating configured stores.

    Observations are per SKU/store. Cached data is labelled stale and is never
    supplied as fresh availability. No database, scheduler, or notification
    framework is added: the existing Core owns those functions.
    """

    def __init__(self, settings=None, *, transport=None, clock=None, budget=None):
        self.settings = settings if isinstance(settings, dict) else {}
        self.region = self.settings.get("region")
        self.status = "APPLE_TARGETS_NOT_CONFIGURED"
        self.inventory = []
        self.targets = []
        self._transport = transport or _http_get
        self.budget = budget
        self._clock = clock or time.monotonic
        self._next_request = 0.0
        self._cursor = 0
        self._lock = asyncio.Lock()
        self._observations = {}
        self._blocked = False
        interval = self.settings.get("interval_seconds", 60)
        self.interval = max(60, interval) if type(interval) in (int, float) and 0 < interval < 86400 else 60
        raw = self.settings.get("targets", [])
        if self.settings.get("enabled") is False:
            self.status = "DISABLED"
            return
        if not isinstance(self.region, str) or self.region not in REGIONS or not isinstance(raw, list) or not raw:
            return
        if len(raw) > 40:
            self.status = "APPLE_CONFIG_INVALID"
            return
        seen = set()
        for target in raw:
            if not isinstance(target, dict):
                self.status = "APPLE_CONFIG_INVALID"
                return
            sku, stores = target.get("sku"), target.get("stores", [])
            modes = target.get("modes", ["pickup"] if stores else ["delivery"])
            if (not isinstance(sku, str) or not _SKU.fullmatch(sku) or sku in seen or
                    not isinstance(stores, list) or len(stores) > 20 or len(set(str(s) for s in stores)) != len(stores) or
                    any(not isinstance(s, str) or not _STORE.fullmatch(s) for s in stores) or
                    not isinstance(modes, list) or not modes or any(m not in ("pickup", "delivery") for m in modes)):
                self.status = "APPLE_CONFIG_INVALID"
                return
            if "pickup" in modes and not stores:
                return
            if "delivery" in modes and not target.get("location"):
                return
            url = target.get("product_url")
            if url is not None and not _product_url(self.region, url):
                self.status = "APPLE_CONFIG_INVALID"
                return
            # Watch case/band combinations need an independently verified model.
            if str(target.get("product_family", "")).lower() in ("watch", "apple watch"):
                self.status = "APPLE_WATCH_CONFIGURATION_UNVERIFIED"
                return
            seen.add(sku)
            self.targets.append({**target, "stores": stores, "modes": modes})
        self.status = "CONFIGURED"

    def _groups(self):
        stores = sorted({s for t in self.targets if "pickup" in t["modes"] for s in t["stores"]})
        return [(store, [t["sku"] for t in self.targets if store in t["stores"] and "pickup" in t["modes"]]) for store in stores]

    async def poll(self):
        async with self._lock:
            if self.status in ("APPLE_CONFIG_INVALID", "APPLE_TARGETS_NOT_CONFIGURED", "DISABLED", "APPLE_WATCH_CONFIGURATION_UNVERIFIED"):
                self.inventory = []
                return []
            groups, fresh = self._groups(), set()
            now = self._clock()
            if self._blocked:
                self.status = "HTTP_BLOCKED"
            elif not groups:
                self.status = "DELIVERY_UNVERIFIED"
            elif not self.budget and now < self._next_request:
                self.status = "RATE_LIMIT_WAIT"
            else:
                ticket, admitted = None, True
                try:
                    if self.budget:
                        ticket = self.budget.claim("apple", self.region, "pickup", interval_seconds=self.interval)
                except BudgetWait:
                    self.status, admitted = "RATE_LIMIT_WAIT", False
                if admitted:
                    store, skus = groups[self._cursor % len(groups)]
                    self._cursor += 1
                    params = {"pl": "true", "mts.0": "regular", "store": store}
                    params.update({f"parts.{i}": sku for i, sku in enumerate(skus)})
                    url = REGIONS[self.region] + "/shop/retail/pickup-message?" + urlencode(params)
                    self._next_request = now + self.interval
                    try:
                        status, payload, retry = await asyncio.to_thread(self._transport, url)
                    except Exception:
                        status, payload, retry = None, None, None
                    self.status = "OBSERVED" if status == 200 else "RATE_LIMITED" if status == 429 else "HTTP_BLOCKED" if status in (401, 403, 541) else "NETWORK_ERROR"
                    if not self.budget and status in (401, 403, 541):
                        self._blocked = True
                    if not self.budget and status == 429:
                        wait = max(300, int(retry)) if isinstance(retry, str) and retry.isdigit() and len(retry) < 8 else 300
                        self._next_request = self._clock() + wait
                    for sku in skus:
                        key = sku, store
                        observed = parse_pickup(payload, sku, store) if status == 200 else PickupObservation(sku, store, status=self.status)
                        self._observations[key] = observed
                        fresh.add(key)
                    if status == 200 and all(self._observations[key].availability == "UNKNOWN" for key in fresh):
                        self.status = "DATA_UNVERIFIED"
                    if ticket:
                        accepted = (self.budget.success(ticket) if self.status == "OBSERVED" else
                            self.budget.failure(ticket, self.status, retry_after=retry, limited=status in (401, 403, 429, 541)))
                        if not accepted:
                            self.status = "RATE_PROBE_EXPIRED"
                            fresh.clear()
            result = []
            for target in self.targets:
                sku = target["sku"]
                if "pickup" in target["modes"]:
                    for store in target["stores"]:
                        observation = self._observations.get((sku, store), PickupObservation(sku, store))
                        is_fresh = (sku, store) in fresh
                        result.append({"sku": sku, "region": self.region, "mode": "pickup", "store_id": store,
                            "availability": observation.availability if is_fresh else "UNKNOWN",
                            "status": observation.status if is_fresh else "STALE" if (sku, store) in self._observations else "NOT_OBSERVED",
                            "fresh": is_fresh, "name": observation.name})
                if "delivery" in target["modes"]:
                    result.append({"sku": sku, "region": self.region, "mode": "delivery", "availability": "UNKNOWN", "status": "DELIVERY_UNVERIFIED", "fresh": False})
            self.inventory = result
            return result


class AppleProvider:
    provider_name = "apple"

    def __init__(self, settings=None, *, log=None, transport=None, clock=None, budget=None):
        self.settings = dict(settings or {})
        self.budget = budget
        self.monitor = AppleInventoryMonitor(settings, transport=transport, clock=clock, budget=budget)
        self.log = log
        self.details = {}
        self.catalog = None
        self.catalog_products = []
        self.catalog_status = "DISABLED"
        self.catalog_next = 0
        self.catalog_clock = clock or time.monotonic
        self.catalog_blocked = False
        if self.settings.get("catalog_enabled") is True:
            from .apple_catalog import AppleCatalog
            categories = self.settings.get("catalog_categories")
            interval = self.settings.get("catalog_refresh_seconds", 3600)
            if (not isinstance(categories, list) or not 1 <= len(categories) <= 8
                    or any(not isinstance(c, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", c) for c in categories)
                    or type(interval) not in (int, float) or not 3600 <= interval <= 86400):
                raise AutoGrabError("APPLE_CATALOG_SCOPE_NOT_CONFIGURED")
            self.catalog = AppleCatalog(self.settings.get("region"), budget=budget)
            self.catalog_status = "PENDING"

    @property
    def status(self):
        return self.monitor.status

    @property
    def inventory(self):
        return self.monitor.inventory

    @property
    def rate_status(self):
        region = self.monitor.region
        if not self.budget or region not in REGIONS:
            return None
        pickup = self.budget.status("apple", region, "pickup")
        if self.catalog:
            catalog = self.budget.status("apple", region, "catalog")
            if catalog["blocked_until"] > pickup["blocked_until"]:
                return catalog
        return pickup

    async def discover_products(self):
        inventory = await self.monitor.poll()
        catalog_products = []
        if self.catalog and not self.catalog_blocked and self.catalog_clock() >= self.catalog_next:
            self.catalog_next = self.catalog_clock() + self.settings.get("catalog_refresh_seconds", 3600)
            try:
                catalog_products = await asyncio.to_thread(self.catalog.refresh, self.settings["catalog_categories"])
                self.catalog_products = catalog_products
                self.catalog_status = "VERIFIED"
            except AutoGrabError as error:
                self.catalog_status = error.code
                self.catalog_blocked = not self.budget and error.code in {"HUMAN_CHALLENGE_REQUIRED", "RATE_LIMITED"}
                if self.budget:
                    wait = self.budget.status("apple", self.monitor.region, "catalog")["wait_seconds"]
                    self.catalog_next = self.catalog_clock() + max(1, wait)
                if self.log:
                    self.log.write("APPLE_CATALOG_PAUSED", code=error.code)
                if not inventory:
                    raise
        if not inventory and not catalog_products:
            if self.catalog_products and not self.catalog_blocked:
                return self.catalog_products
            raise AutoGrabError(self.catalog_status if self.catalog else self.status)
        products = []
        for target in self.monitor.targets:
            sku = target["sku"]
            entries = [item for item in inventory if item["sku"] == sku]
            states = [item["availability"] for item in entries]
            state = "AVAILABLE" if "AVAILABLE" in states else "SOLD_OUT" if states and all(s == "SOLD_OUT" for s in states) else "UNKNOWN"
            product_id = f"{self.monitor.region}:{sku}"
            metadata = {"inventory": entries, "inventory_status": self.status,
                "configured_target": True, "catalog_discovered": False,
                "observed_at": datetime.now(timezone.utc).isoformat(), "preorder": "UNKNOWN",
                "delivery": "UNKNOWN", "checkout": "UNVERIFIED"}
            self.details[product_id] = metadata
            kwargs = {"metadata": metadata} if "metadata" in Product.__dataclass_fields__ else {}
            url = target.get("product_url") or REGIONS[self.monitor.region] + "/shop"
            name = next((e["name"] for e in entries if e.get("name")), sku)
            products.append(Product(product_id=product_id, name=name, availability=state,
                prices=[], product_url=url, categories=["Apple", str(target.get("product_family", "Configured SKU"))],
                locations=target["stores"], eligible=True, order_url=None, provider="apple", **kwargs))
        if catalog_products:
            from .apple_catalog import merge_observations
            return merge_observations([*catalog_products, *products], [])
        return products

    async def check_product(self, product_id):
        for product in await self.discover_products():
            if product.product_id == product_id:
                return product
        raise AutoGrabError("PRODUCT_MISSING")

    async def check_stock(self, product_id):
        return (await self.check_product(product_id)).availability

    async def get_product_details(self, product_id):
        return await self.check_product(product_id)

    async def open_product(self, product):
        raise AutoGrabError("EDGE_COMPANION_REQUIRED")

    async def prepare_cart(self, product):
        raise AutoGrabError("APPLE_CHECKOUT_UNVERIFIED")

    async def dry_run_checkout(self, product, cart):
        raise AutoGrabError("APPLE_CHECKOUT_UNVERIFIED")
