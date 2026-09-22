"""VMISS public catalog discovery; normal Edge owns authenticated execution.

The current app.vmiss.com store requires human verification from this network.
The Lagom card contract below is experimental until verified against that live
store. No external monitor code or browser session is imported.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from autograb.core.rate_budget import PublicHTTPError
from .dmit import _Document

ORIGIN = "https://app.vmiss.com"
CATALOG_URL = ORIGIN + "/store"
MAX_PAGE_BYTES = 2_000_000
MAX_GROUPS = 40


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _normal_document(html):
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_PAGE_BYTES:
        raise AutoGrabError("CATALOG_INVALID")
    doc = _Document(html)
    text = doc.root.text()
    if re.search(r"\berror\s*1015\b|\byou are being rate limited\b", text, re.I):
        raise AutoGrabError("RATE_LIMITED")
    if re.search(r"just a moment|verify (?:that )?you are (?:a )?human|checking your browser", text, re.I):
        raise AutoGrabError("HUMAN_CHALLENGE_REQUIRED")
    return doc


def _http_error_code(error):
    if error.code == 429:
        return "RATE_LIMITED"
    if error.code == 403:
        # A 403 can be a challenge, a rate-limit page or a plain denial. Read
        # only the returned public body; never repeat the blocked request.
        try:
            _normal_document(error.read(MAX_PAGE_BYTES + 1).decode("utf-8", errors="replace"))
        except AutoGrabError as classified:
            if classified.code in {"RATE_LIMITED", "HUMAN_CHALLENGE_REQUIRED"}:
                return classified.code
        except (OSError, ValueError):
            pass
        return "HTTP_403"
    return "CATALOG_UNAVAILABLE"


def _store_url(value, base=CATALOG_URL):
    if not isinstance(value, str):
        return None
    parts = urlsplit(urljoin(base, value))
    if (parts.scheme != "https" or parts.netloc != "app.vmiss.com" or parts.query or parts.fragment
            or not re.fullmatch(r"/store(?:/[a-z0-9][a-z0-9-]{0,79}){0,2}/?", parts.path)):
        return None
    return ORIGIN + parts.path.rstrip("/")


def _by_class(node, name):
    return [child for child in node.nodes() if name in child.attrs.get("class", "").split()]


def _parse_page(html, page_url=CATALOG_URL):
    if _store_url(page_url) != page_url.rstrip("/"):
        raise AutoGrabError("CATALOG_URL_INVALID")
    doc = _normal_document(html)
    groups = set()
    for node in doc.root.nodes():
        link = _store_url(node.attrs.get("href"), page_url) if node.tag == "a" else None
        if link and len(urlsplit(link).path.strip("/").split("/")) == 2:
            groups.add(link)
    cards = _by_class(doc.root, "package")
    products = {}
    for card in cards:
        names = _by_class(card, "package-title")
        buttons = _by_class(card, "btn-order-now")
        if len(names) != 1 or len(buttons) != 1:
            raise AutoGrabError("SITE_CHANGED")
        button = buttons[0]
        order_url = _store_url(button.attrs.get("href"), page_url)
        if not order_url or len(urlsplit(order_url).path.strip("/").split("/")) != 3:
            raise AutoGrabError("CATALOG_PRODUCT_ID_MISSING")
        product_id = urlsplit(order_url).path.removeprefix("/store/")
        name = names[0].text()
        if not name or len(name) > 200 or product_id in products:
            raise AutoGrabError("CATALOG_IDENTITY_INVALID")
        disabled = ("disabled" in button.attrs or button.attrs.get("aria-disabled") == "true"
                    or "disabled" in button.attrs.get("class", "").split())
        quantities = _by_class(card, "package-qty")
        count = re.fullmatch(r"([0-9]+)\s+Available", quantities[0].text(), re.I) if len(quantities) == 1 else None
        state = "UNKNOWN"
        sold_out = bool(re.search(r"\b(?:out of stock|sold out)\b", card.text(), re.I))
        if count and int(count[1]) > 0 and not disabled and not sold_out:
            state = "AVAILABLE"
        elif count and int(count[1]) == 0 and disabled:
            state = "SOLD_OUT"
        elif disabled and sold_out and (not count or int(count[1]) == 0):
            state = "SOLD_OUT"
        amounts, cycles = _by_class(card, "price-amount"), _by_class(card, "price-cycle")
        prices = []
        if len(amounts) == 1 and len(cycles) == 1:
            price = re.search(r"([0-9]+(?:,[0-9]{3})*\.[0-9]{2})\s*(CAD|USD|EUR|GBP)\b", amounts[0].text())
            period = cycles[0].text().strip(" /-").lower()
            if price and period in {"monthly", "quarterly", "semiannually", "annually", "biennially", "triennially"}:
                prices = [{"cents": int(Decimal(price[1].replace(",", "")) * 100),
                           "currency": price[2], "period": period, "available": True}]
        group = product_id.split("/")[0]
        products[product_id] = Product(product_id=product_id, name=name, availability=state,
            prices=prices, product_url=ORIGIN + "/store/" + group, categories=[group],
            eligible=True, order_url=order_url, provider="vmiss")
    if not products and not groups:
        raise AutoGrabError("SITE_CHANGED")
    return list(products.values()), sorted(groups)


def parse_catalog(html, page_url=CATALOG_URL):
    return _parse_page(html, page_url)[0]


class VMISSProvider:
    provider_name = "vmiss"
    source = CATALOG_URL

    def __init__(self, browser=None, log=None, *, budget=None, settings=None, clock=None):
        self.browser, self.log = browser, log
        self.budget = budget
        self._clock = clock or time.time
        interval = (settings or {}).get("interval_seconds", 900)
        self.interval = max(900, interval) if type(interval) in (int, float) and 0 < interval <= 86400 else 900
        self.last_products = []
        self._completed_at = None
        self._reset_pending()

    def _reset_pending(self):
        self._queue, self._visited, self._pending = [CATALOG_URL], set(), {}

    @property
    def rate_status(self):
        return self.budget.status("vmiss", "global", "catalog") if self.budget else None

    def _http(self, url):
        if not _store_url(url):
            raise AutoGrabError("CATALOG_URL_INVALID")
        try:
            with build_opener(_NoRedirect).open(Request(url, headers={
                    "User-Agent": "AutoGrab/0.4 (public inventory monitor; DRY_RUN)",
                    "Accept": "text/html"}), timeout=15) as response:
                raw = response.read(MAX_PAGE_BYTES + 1)
                if response.status != 200 or len(raw) > MAX_PAGE_BYTES:
                    raise AutoGrabError("CATALOG_UNAVAILABLE")
                html = raw.decode("utf-8", errors="strict")
                try:
                    _normal_document(html)
                except AutoGrabError as error:
                    raise PublicHTTPError(error.code, response.headers.get("Retry-After")) from None
                return html
        except HTTPError as error:
            raise PublicHTTPError(_http_error_code(error), error.headers.get("Retry-After")) from None
        except (URLError, OSError, UnicodeError):
            raise AutoGrabError("NETWORK_ERROR") from None

    async def discover_products(self):
        if self.budget:
            return await self._discover_budgeted()
        queue, visited, products = [CATALOG_URL], set(), {}
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            if len(visited) >= MAX_GROUPS:
                raise AutoGrabError("CATALOG_LIMIT_REACHED")
            visited.add(url)
            page, groups = _parse_page(await asyncio.to_thread(self._http, url), url)
            for product in page:
                previous = products.get(product.product_id)
                if previous and previous != product:
                    raise AutoGrabError("CATALOG_IDENTITY_CONFLICT")
                products[product.product_id] = product
            queue.extend(group for group in groups if group not in visited)
        if not products:
            raise AutoGrabError("CATALOG_EMPTY")
        self.last_products = list(products.values())
        return self.last_products

    async def _discover_budgeted(self):
        # One actual request per shared slot, not one request for every product.
        # A multi-page scan is private scratch until the complete catalogue exists.
        ticket = self.budget.claim("vmiss", "global", "catalog", interval_seconds=self.interval)
        self._completed_at = None
        if ticket.probe:
            self._reset_pending()
        url = self._queue.pop(0)
        try:
            page, groups = _parse_page(await asyncio.to_thread(self._http, url), url)
            self._visited.add(url)
            for product in page:
                previous = self._pending.get(product.product_id)
                if previous and previous[0] != product:
                    raise AutoGrabError("CATALOG_IDENTITY_CONFLICT")
                self._pending[product.product_id] = product, url
            self._queue.extend(group for group in groups if group not in self._visited and group not in self._queue)
            if len(self._visited) + len(self._queue) > MAX_GROUPS:
                raise AutoGrabError("CATALOG_LIMIT_REACHED")
            if not self._queue and not self._pending:
                raise AutoGrabError("CATALOG_EMPTY")
        except Exception as error:
            code = error.code if isinstance(error, AutoGrabError) else "NETWORK_ERROR"
            self.budget.failure(ticket, code, retry_after=getattr(error, "retry_after", None),
                limited=code in {"RATE_LIMITED", "HTTP_403", "HUMAN_CHALLENGE_REQUIRED"})
            self._reset_pending()
            raise AutoGrabError(code) from None
        if not self.budget.success(ticket):
            self._reset_pending()
            raise AutoGrabError("RATE_PROBE_EXPIRED")
        if self._queue:
            raise AutoGrabError("CATALOG_INCOMPLETE")
        now = self._clock()
        self.last_products = [p if page_url == url else replace(p, availability="UNKNOWN")
                              for p, page_url in self._pending.values()]
        self._completed_at = now
        self._reset_pending()
        return self.last_products

    async def get_product_details(self, product_id):
        if self.budget:
            if self._completed_at is None or self._clock() - self._completed_at >= self.interval:
                raise AutoGrabError("STOCK_UNKNOWN")
            products = self.last_products
        else:
            products = await self.discover_products()
        for product in products:
            if product.product_id == product_id:
                return product
        raise AutoGrabError("PRODUCT_MISSING")

    async def check_product(self, product_id):
        product = await self.get_product_details(product_id)
        if product.availability != "AVAILABLE":
            raise AutoGrabError("OUT_OF_STOCK" if product.availability == "SOLD_OUT" else "STOCK_UNKNOWN")
        return product

    async def check_stock(self, product_id):
        return (await self.get_product_details(product_id)).availability

    async def open_product(self, product):
        raise AutoGrabError("EDGE_COMPANION_REQUIRED")

    async def prepare_cart(self, product):
        raise AutoGrabError("EDGE_COMPANION_REQUIRED")

    async def dry_run_checkout(self, product, cart):
        raise AutoGrabError("EDGE_COMPANION_REQUIRED")
