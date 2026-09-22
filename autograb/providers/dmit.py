"""Public DMIT discovery; authenticated actions belong to Edge Companion.

HTTP was challenge-blocked; normal Edge exposed the public dmit_cart_2020 theme
and its official selection script. Both that observed theme and the published
WHMCS standard_cart product contract are parsed conservatively. An unrecognized
page fails closed. No third-party monitor code is incorporated.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from html.parser import HTMLParser
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from autograb.core.errors import AutoGrabError
from autograb.core.models import Product

ORIGIN = "https://www.dmit.io"
CATALOG_URL = ORIGIN + "/cart.php"
MAX_PAGE_BYTES = 2_000_000
MAX_GROUPS = 60
_PID = re.compile(r"[1-9][0-9]{0,39}\Z")
_CYCLES = ("monthly", "quarterly", "semiannually", "annually", "biennially", "triennially")


class _Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def nodes(self):
        if self.hidden():
            return
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.nodes()

    def text(self):
        if self.hidden():
            return ""
        return " ".join(" ".join(c.text() if isinstance(c, _Node) else c for c in self.children).split())

    def hidden(self):
        return (self.tag in {"script", "style", "template"} or "hidden" in self.attrs
                or self.attrs.get("aria-hidden") == "true"
                or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", self.attrs.get("style", ""), re.I)))


class _Document(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not any(n.tag in {"script", "style", "template"} for n in self.stack):
            self.stack[-1].children.append(data)


def _catalog_url(value, base=CATALOG_URL):
    url = urlsplit(urljoin(base, value))
    query = parse_qs(url.query, keep_blank_values=True)
    if (url.scheme != "https" or url.netloc != "www.dmit.io" or url.path != "/cart.php"
            or url.fragment or set(query) - {"gid", "language", "currency"}
            or any(len(v) != 1 for v in query.values())):
        return None
    if "gid" in query and not _PID.fullmatch(query["gid"][0]):
        return None
    if "currency" in query and not _PID.fullmatch(query["currency"][0]):
        return None
    if "language" in query and query["language"] != ["english"]:
        return None
    return CATALOG_URL + ("?" + urlencode(sorted((k, v[0]) for k, v in query.items())) if query else "")


def _order_url(value, product_id, base):
    url = urlsplit(urljoin(base, value))
    query = parse_qs(url.query, keep_blank_values=True)
    if (url.scheme != "https" or url.netloc != "www.dmit.io" or url.path != "/cart.php"
            or url.fragment or query.get("a") != ["add"] or query.get("pid") != [product_id]
            or any(len(v) != 1 for v in query.values())
            or any(k not in {"a", "pid", "language", "currency", "billingcycle"}
                   and not re.fullmatch(r"configoption\[[0-9]+\]", k) for k in query)):
        raise AutoGrabError("CATALOG_INVALID_ORDER_URL")
    if "billingcycle" in query and query["billingcycle"][0] not in _CYCLES:
        raise AutoGrabError("CATALOG_INVALID_ORDER_URL")
    if "language" in query and query["language"] != ["english"]:
        raise AutoGrabError("CATALOG_INVALID_ORDER_URL")
    if any(not _PID.fullmatch(values[0]) for key, values in query.items()
           if key == "currency" or key.startswith("configoption[")):
        raise AutoGrabError("CATALOG_INVALID_ORDER_URL")
    return url.geturl()


def _stock(card):
    quantities = [n.text() for n in card.nodes() if "qty" in n.attrs.get("class", "").split()]
    counts = [int(m.group(1)) for text in quantities if (m := re.fullmatch(r"([0-9]+)\s+Available", text, re.I))]
    statuses = [n.text().casefold() for n in card.nodes()
                if set(n.attrs.get("class", "").split()) & {"stock", "stock-status", "availability"}]
    out = any(s in {"out of stock", "sold out"} for s in statuses) or 0 in counts
    available = any(count > 0 for count in counts) or "in stock" in statuses
    # A purchase link is not stock evidence: WHMCS renders it even at zero stock.
    return "UNKNOWN" if out == available else "SOLD_OUT" if out else "AVAILABLE"


def _prices(card, product_id):
    containers = [n for n in card.nodes() if n.attrs.get("id") == f"product{product_id}-price"]
    if len(containers) != 1:
        return []
    container = containers[0]
    amounts = [n.text() for n in container.nodes() if "price" in n.attrs.get("class", "").split()]
    if len(amounts) != 1:
        return []
    money = amounts[0]
    match = re.fullmatch(r"\s*(?:[A-Z]{3}\s*)?(?:US\$|\$|€|£)?\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:[A-Z]{3})?\s*", money)
    currencies = re.findall(r"\b(?:USD|EUR|GBP|CAD|CNY|JPY|HKD)\b", money)
    currency = currencies[0] if len(set(currencies)) == 1 else "USD" if "US$" in money else None
    periods = [cycle for cycle in _CYCLES if re.search(r"\b" + cycle + r"\b", container.text(), re.I)]
    cents = int(Decimal(match.group(1)) * 100) if match else None
    return [{"cents": cents, "currency": currency,
             "period": periods[0] if len(periods) == 1 else None,
             "available": None if cents is None else True}]


def _dmit_price(text):
    match = re.fullmatch(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s+USD\s*/\s*(Monthly|Quarterly|Semiannually|Annually|Biennially|Triennially)", text, re.I)
    if not match:
        return []
    return [{"cents": int(Decimal(match[1]) * 100), "currency": "USD",
             "period": match[2].lower(), "available": True}]


def parse_public_snapshot(rows, page_url=CATALOG_URL):
    """Map only the six public fields observed on DMIT's normal Edge catalog.

    `disabled` is the observed `.none-stock` class, not an inferred stock flag.
    The official theme script excludes that class when enabling Continue; a
    nonempty price is also required. This is page availability, not cart proof.
    """
    if not _catalog_url(page_url) or not isinstance(rows, list) or not rows:
        raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
    products, seen = [], set()
    for item in rows:
        if (type(item) is not dict or set(item) != {"pid", "name", "gid", "stock", "disabled", "price"}
                or not all(isinstance(item[key], str) for key in ("pid", "name", "gid", "stock", "price"))
                or not _PID.fullmatch(item["pid"]) or not _PID.fullmatch(item["gid"])
                or item["pid"] in seen or type(item["disabled"]) is not bool
                or not item["name"].strip() or len(item["name"]) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in item["name"])):
            raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
        seen.add(item["pid"])
        stock = " ".join(item["stock"].split()).casefold()
        prices = _dmit_price(" ".join(item["price"].split()))
        if item["disabled"] or stock in {"out of stock", "sold out"}:
            availability = "SOLD_OUT"
        elif prices and (not stock or re.fullmatch(r"[1-9][0-9]*\s+available", stock)):
            availability = "AVAILABLE"
        else:
            availability = "UNKNOWN"
        products.append(Product(item["pid"], " ".join(item["name"].split()), availability, prices,
                                page_url, categories=["gid:" + item["gid"]], eligible=True,
                                provider="dmit"))
    return products


def _dmit_theme(document, page_url):
    cards = [n for n in document.root.nodes() if "cart-products-item" in n.attrs.get("class", "").split()]
    if not cards:
        return None
    rows = []
    for card in cards:
        def matches(class_name):
            return [n for n in card.nodes() if class_name in n.attrs.get("class", "").split()]
        boxes, names, prices, quantities = (matches(name) for name in
                    ("cart-products-box", "cart-products-title", "cart-products-price", "cart-products-qty"))
        if len(boxes) != 1 or len(names) != 1 or len(prices) != 1 or len(quantities) > 1:
            raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
        rows.append({"pid": boxes[0].attrs.get("pid"), "name": names[0].text(),
                     "gid": card.attrs.get("gid"), "price": prices[0].text(),
                     "stock": quantities[0].text() if quantities else "",
                     "disabled": "none-stock" in boxes[0].attrs.get("class", "").split()})
    # The observed theme embeds all groups and filters locally; no group fetches.
    return parse_public_snapshot(rows, page_url), []


def _parse_page(html, page_url):
    if not isinstance(html, str) or not _catalog_url(page_url):
        raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
    lower = html.casefold()
    if any(s in lower for s in ("cf-chl-", "just a moment", "verify you are human", "performing security verification")):
        raise AutoGrabError("HUMAN_CHALLENGE_REQUIRED")
    document = _Document(html)
    custom = _dmit_theme(document, page_url)
    if custom is not None:
        return custom
    nodes = list(document.root.nodes())
    roots = [n for n in nodes if n.attrs.get("id") == "products"]
    if len(roots) != 1:
        raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
    group_links = set()
    for node in nodes:
        if node.tag == "a" and (link := _catalog_url(node.attrs.get("href", ""), page_url)):
            if "gid" in parse_qs(urlsplit(link).query):
                group_links.add(link)
    group = parse_qs(urlsplit(page_url).query).get("gid", [])
    categories = [f"gid:{group[0]}"] if group else []
    products = []
    seen = set()
    for card in roots[0].nodes():
        identity = card.attrs.get("id", "")
        if not re.fullmatch(r"product[1-9][0-9]{0,39}", identity):
            continue
        pid = identity[7:]
        names = [n.text() for n in card.nodes() if n.attrs.get("id") == identity + "-name"]
        if pid in seen or len(names) != 1 or not names[0] or len(names[0]) > 200:
            raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
        seen.add(pid)
        links = [n.attrs.get("href") for n in card.nodes()
                 if n.tag == "a" and n.attrs.get("id") == identity + "-order-button"]
        if len(links) > 1 or (links and not links[0]):
            raise AutoGrabError("CATALOG_INVALID_ORDER_URL")
        order_url = _order_url(links[0], pid, page_url) if links else None
        products.append(Product(product_id=pid, name=names[0], availability=_stock(card),
                                prices=_prices(card, pid), product_url=page_url,
                                categories=categories, eligible=True, order_url=order_url,
                                provider="dmit"))
    if not products:
        raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
    return products, sorted(group_links)


def parse_catalog(html, page_url=CATALOG_URL):
    """Parse one recognized catalog page; missing stock remains UNKNOWN."""
    return _parse_page(html, page_url)[0]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class DMITProvider:
    provider_name = "dmit"

    def __init__(self, browser=None, log=None):
        self.log = log
        self.source = None
        self.last_products = []

    def _http(self, url):
        if not _catalog_url(url):
            raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
        request = Request(url, headers={"Accept": "text/html", "User-Agent": "AutoGrab/0.3 (public inventory monitor)"})
        try:
            response = build_opener(_NoRedirect).open(request, timeout=15)
        except HTTPError as error:
            response = error
        except Exception:
            raise AutoGrabError("NETWORK_ERROR") from None
        with response:
            body = response.read(MAX_PAGE_BYTES + 1)
            if len(body) > MAX_PAGE_BYTES:
                raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
            return response.code, body.decode("utf-8", "replace")

    async def discover_products(self):
        queue, visited, catalog = [CATALOG_URL], set(), {}
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            if len(visited) >= MAX_GROUPS:
                raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
            visited.add(url)
            status, html = await asyncio.to_thread(self._http, url)
            if status == 403:
                raise AutoGrabError("HUMAN_CHALLENGE_REQUIRED")
            if status == 429:
                raise AutoGrabError("RATE_LIMITED")
            if status != 200:
                raise AutoGrabError("NETWORK_ERROR")
            products, groups = _parse_page(html, url)
            for product in products:
                if previous := catalog.get(product.product_id):
                    if any(getattr(previous, field) != getattr(product, field)
                           for field in ("name", "availability", "prices", "order_url")):
                        raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
                    product = replace(previous, categories=sorted(set(previous.categories + product.categories)))
                catalog[product.product_id] = product
            queue.extend(group for group in groups if group not in visited and group not in queue)
        self.last_products = list(catalog.values())
        self.source = "PUBLIC_HTTP"
        if self.log:
            self.log.write("DATA_ROUTE", route="PUBLIC_HTTP", status="SUPPORTED", products=len(catalog))
        return self.last_products

    async def get_product_details(self, product_id):
        products = await self.discover_products()
        for product in products:
            if product.product_id == product_id:
                return product
        raise AutoGrabError("PRODUCT_MISSING")

    async def check_stock(self, product_id):
        return (await self.get_product_details(product_id)).availability

    async def check_product(self, product_id):
        product = await self.get_product_details(product_id)
        if product.availability != "AVAILABLE":
            raise AutoGrabError("OUT_OF_STOCK" if product.availability == "SOLD_OUT" else "STOCK_UNKNOWN")
        return product

    async def open_product(self, product):
        raise AutoGrabError("EDGE_COMPANION_REQUIRED")

    async def prepare_cart(self, product):
        raise AutoGrabError("EDGE_COMPANION_REQUIRED")

    async def dry_run_checkout(self, product, cart):
        raise AutoGrabError("EDGE_COMPANION_REQUIRED")
