"""V.PS public family catalogs and real HostBill product identifiers.

Official marketing pages establish plans, prices and order links, not inventory.
Absent explicit stock evidence the availability remains UNKNOWN. Cart execution
must be observed independently in normal Edge; no HTTP add/checkout is sent.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, build_opener

from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from .vmiss import _NoRedirect, _normal_document, _http_error_code, MAX_PAGE_BYTES

ORIGIN = "https://v.ps"
ORDER_ORIGIN = "https://vps.hosting"
CATALOG_URL = ORIGIN + "/vps/"


def _family_url(value, base=CATALOG_URL):
    if not isinstance(value, str):
        return None
    parts = urlsplit(urljoin(base, value))
    if (parts.scheme != "https" or parts.netloc != "v.ps" or parts.query or parts.fragment
            or not re.fullmatch(r"/products/[a-z0-9-]+-kvm-vps/", parts.path)):
        return None
    return ORIGIN + parts.path


def _order(value):
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    query = parse_qs(parts.query, keep_blank_values=True)
    if (parts.scheme != "https" or parts.netloc != "vps.hosting" or parts.path != "/" or parts.fragment
            or set(query) != {"cmd", "action", "id"} or query.get("cmd") != ["cart"]
            or query.get("action") != ["add"] or len(query.get("id", [])) != 1
            or not re.fullmatch(r"[1-9][0-9]{0,39}", query["id"][0])):
        return None
    return query["id"][0], f"{ORDER_ORIGIN}/?cmd=cart&action=add&id={query['id'][0]}"


def discover_groups(html):
    doc = _normal_document(html)
    groups = {_family_url(node.attrs.get("href")) for node in doc.root.nodes() if node.tag == "a"}
    groups.discard(None)
    if not groups or len(groups) > 30:
        raise AutoGrabError("SITE_CHANGED")
    return sorted(groups)


def parse_catalog(html, page_url):
    if _family_url(page_url) != page_url:
        raise AutoGrabError("CATALOG_URL_INVALID")
    doc = _normal_document(html)
    names = [node.text() for node in doc.root.nodes() if node.tag == "h1"]
    if len(names) != 1 or not names[0]:
        raise AutoGrabError("SITE_CHANGED")
    group = page_url.rstrip("/").split("/")[-1]
    products = {}
    for node in doc.root.nodes():
        identity = _order(node.attrs.get("href")) if node.tag == "a" else None
        if identity is None:
            continue
        pid, order_url = identity
        title = node.attrs.get("title", "")
        if not title.startswith("Order ") or not title.removeprefix("Order ").strip():
            raise AutoGrabError("CATALOG_IDENTITY_INVALID")
        name = title.removeprefix("Order ").strip()
        if not node.text().startswith(name) or len(name) > 200 or pid in products:
            raise AutoGrabError("CATALOG_IDENTITY_INVALID")
        prices = re.findall(r"€\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})\s*/\s*(mo|yr)\b", node.text())
        quote = []
        if len(prices) == 1:
            amount, period = prices[0]
            quote = [{"cents": int(Decimal(amount.replace(",", "")) * 100), "currency": "EUR",
                      "period": "monthly" if period == "mo" else "annually", "available": True}]
        statuses = {child.text().casefold() for child in node.nodes()
                    if set(child.attrs.get("class", "").split()) & {"stock", "availability", "stock-status"}}
        out = bool(statuses & {"out of stock", "sold out"})
        available = "in stock" in statuses
        disabled = ("disabled" in node.attrs or node.attrs.get("aria-disabled") == "true"
                    or "disabled" in node.attrs.get("class", "").split())
        state = "UNKNOWN" if out == available else "SOLD_OUT" if out else "UNKNOWN" if disabled else "AVAILABLE"
        products[pid] = Product(product_id=pid, name=name, availability=state,
            prices=quote,
            product_url=page_url, categories=[group], locations=[name.split()[0]],
            eligible=True, order_url=order_url, provider="vps")
    if not products:
        raise AutoGrabError("SITE_CHANGED")
    return list(products.values())


class VPSProvider:
    provider_name = "vps"
    source = CATALOG_URL

    def __init__(self, browser=None, log=None):
        self.browser, self.log = browser, log
        self.last_products = []

    def _http(self, url):
        if url != CATALOG_URL and not _family_url(url):
            raise AutoGrabError("CATALOG_URL_INVALID")
        try:
            with build_opener(_NoRedirect).open(Request(url, headers={
                    "User-Agent": "AutoGrab/0.4 (public inventory monitor; DRY_RUN)",
                    "Accept": "text/html"}), timeout=15) as response:
                raw = response.read(MAX_PAGE_BYTES + 1)
                if response.status != 200 or len(raw) > MAX_PAGE_BYTES:
                    raise AutoGrabError("CATALOG_UNAVAILABLE")
                return raw.decode("utf-8", errors="strict")
        except HTTPError as error:
            raise AutoGrabError(_http_error_code(error)) from None
        except (URLError, OSError, UnicodeError):
            raise AutoGrabError("NETWORK_ERROR") from None

    async def discover_products(self):
        groups = discover_groups(await asyncio.to_thread(self._http, CATALOG_URL))
        products = {}
        for url in groups:
            for product in parse_catalog(await asyncio.to_thread(self._http, url), url):
                if product.product_id in products:
                    raise AutoGrabError("CATALOG_IDENTITY_CONFLICT")
                products[product.product_id] = product
        self.last_products = list(products.values())
        return self.last_products

    async def get_product_details(self, product_id):
        for product in await self.discover_products():
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
