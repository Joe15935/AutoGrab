"""Independent parser for Apple's public current purchase-page JSON.

Protocol research included apple-pickup-watcher (GPL); no upstream code is used.
Model names, variants and store IDs come from official pages, never a SKU list.
"""
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener

from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from .apple import REGIONS, _NoRedirect, _SKU


class PublicPage(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links, self.scripts, self.title = [], [], ""
        self._script, self._anchor, self._h1 = None, None, False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self._anchor = {**attrs, "text": ""}
        if tag == "script":
            self._script = {**attrs, "text": ""}
        if tag == "meta" and attrs.get("property") == "og:title":
            self.title = attrs.get("content", "")
        if tag == "h1" and not self.title:
            self._h1 = True

    def handle_data(self, value):
        if self._script is not None:
            self._script["text"] += value
        if self._anchor is not None:
            self._anchor["text"] += value
        if self._h1:
            self.title += value

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self.links.append(self._anchor); self._anchor = None
        if tag == "script" and self._script is not None:
            self.scripts.append(self._script); self._script = None
        if tag == "h1":
            self._h1 = False


def plain(value):
    if not isinstance(value, str):
        return ""
    # Footnote numbers and secondary explanations are not part of an option.
    value = re.split(r"<(?:span|div)\b[^>]*class=[\"'][^\"']*form-label-small", value, maxsplit=1)[0]
    value = re.sub(r"<as-footnote\b.*?</as-footnote>", "", value, flags=re.S)
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())[:240]


def _official_url(url, region):
    parts, base = urlsplit(url), urlsplit(REGIONS[region])
    return (parts.scheme == "https" and parts.netloc == base.netloc and not parts.query and not parts.fragment
        and re.match(re.escape(base.path) + r"/(?:store|shop|retail)(?:/|$)", parts.path) is not None
        and "/../" not in parts.path and "/./" not in parts.path)


def public_html(url):
    """Bounded GET only, without cookies, auth or redirect outside store pages."""
    region = next((key for key in REGIONS if _official_url(url, key)), None)
    if region is None:
        raise AutoGrabError("APPLE_CATALOG_URL_REJECTED")
    prefix = urlsplit(REGIONS[region]).path
    opener = build_opener(_NoRedirect)
    for _ in range(3):
        request = Request(url, headers={"User-Agent":"AutoGrab/0.4 (public catalog monitor)", "Accept":"text/html"})
        try:
            with opener.open(request, timeout=20) as response:
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise AutoGrabError("APPLE_CATALOG_TOO_LARGE")
                return raw.decode("utf-8")
        except HTTPError as error:
            if error.code in (301, 302, 307, 308):
                destination = urljoin(url, error.headers.get("Location", ""))
                parsed = urlsplit(destination)
                if (not _official_url(destination, region)
                        or not re.fullmatch(re.escape(prefix)+r"/(?:store/?|shop/buy-[a-z0-9-]+(?:/[a-zA-Z0-9-]+)*/?|retail/storelist/?)", parsed.path)):
                    raise AutoGrabError("APPLE_CATALOG_REDIRECT_REJECTED") from None
                url = destination
                continue
            raise AutoGrabError("HUMAN_CHALLENGE_REQUIRED" if error.code in (401, 403, 541) else "RATE_LIMITED" if error.code == 429 else "APPLE_CATALOG_HTTP_ERROR") from None
        except (URLError, OSError, UnicodeError):
            raise AutoGrabError("APPLE_CATALOG_NETWORK_ERROR") from None
    raise AutoGrabError("APPLE_CATALOG_REDIRECT_REJECTED")


def _selection(page):
    found = []
    for script in page.scripts:
        for match in re.finditer(r'(?<![\w])(?:"productSelectionData"|productSelectionData)\s*:', script["text"]):
            try:
                value, _ = json.JSONDecoder().raw_decode(script["text"][match.end():].lstrip())
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("products"), list):
                if value not in found:
                    found.append(value)
    if len(found) != 1:
        raise AutoGrabError("APPLE_CATALOG_SCHEMA_CHANGED")
    return found[0]


def parse_purchase_page(html, url, region):
    """Only real slash-bearing part numbers; configurable family codes are not SKUs."""
    if not _official_url(url, region) or "/shop/buy-" not in urlsplit(url).path:
        raise AutoGrabError("APPLE_CATALOG_URL_REJECTED")
    page, records = PublicPage(html), {}
    data = _selection(page)
    displays = data.get("displayValues") or data.get("mainDisplayValues") or {}
    category = urlsplit(url).path.split("/shop/buy-", 1)[1].split("/")[0]
    family = urlsplit(url).path.rstrip("/").split("/")[-1]
    title = re.sub(r"^(?:购买|購買|选购|選購|Buy)\s*", "", plain(page.title), flags=re.I).split(" - Apple")[0]
    def label(key, value):
        group = displays.get(key, {})
        entry = group.get(value, {}) if isinstance(group, dict) else {}
        return plain(entry.get("value") or entry.get("header") or value) if isinstance(entry, dict) else plain(value)
    for raw in data["products"]:
        if not isinstance(raw, dict):
            raise AutoGrabError("APPLE_CATALOG_SCHEMA_CHANGED")
        sku = next((raw.get(k) for k in ("partNumber", "btrOrFdPartNumber", "part")
                    if isinstance(raw.get(k), str) and _SKU.fullmatch(raw[k])), None)
        if sku is None:
            continue
        dimensions = {k:v for k,v in raw.items() if k.startswith("dimension") and isinstance(v,str)}
        if isinstance(raw.get("dimensions"), dict):
            dimensions.update({k:v for k,v in raw["dimensions"].items() if isinstance(v,str)})
        def dimension(suffix):
            item = next(((k,v) for k,v in dimensions.items() if k.endswith(suffix)), None)
            return label(*item) if item else None
        model = dimension("dimensionScreensize") or title or raw.get("familyType") or family
        if title and model != title and title.lower() not in model.lower() and model.lower() not in title.lower():
            model = title + " · " + model
        capacity = dimension("dimensionCapacity")
        color = dimension("dimensionColor")
        carrier = dimension("dimensionCarrier") or (plain(raw.get("carrierPolicyType")) or None)
        if raw.get("isCarrierDevice") is False and not carrier:
            carrier = "NOT_APPLICABLE"
        product_url = next((urljoin(url, a.get("href", "")) for a in page.links
                            if urljoin(url, a.get("href", "")).lower().rstrip("/").endswith("/" + sku.lower())
                            and _official_url(urljoin(url,a.get("href","")), region)), url)
        prices = []
        price = displays.get("prices", {}).get(raw.get("fullPrice"), {})
        if isinstance(price, dict) and (price.get("product") == sku or sku in price.get("validProducts", [])):
            amount = price.get("amountBeforeTradeIn")
            currency = next((v for marker,v in (("RMB","CNY"),("HK$","HKD"),("US$","USD"),("$","USD"))
                             if marker in str(price.get("currentDisplayPrice", ""))), None)
            if region not in ("cn","us","hk"):
                currency = None
            try:
                cents = Decimal(str(amount)) * 100
                if currency and cents.is_finite() and 0 < cents < 10**12 and cents == cents.to_integral_value():
                    prices = [{"period":"one_time", "cents":int(cents), "currency":currency, "available":None}]
            except InvalidOperation:
                pass
        variants = {"family":family, "model":model, "capacity":capacity, "color":color,
                    "carrier":carrier, "dimensions":dimensions, "family_type":raw.get("familyType")}
        metadata = {"catalog_discovered":True, "catalog_complete":True,
            "catalog_scope":f"{region}:{category}", "catalog_source":url, "catalog_variant":variants,
            "configured_target":False, "order_state":"UNKNOWN", "preorder":"UNKNOWN",
            "pickup_configuration_verified":category != "watch", "inventory":[]}
        product = Product(f"{region}:{sku}", " / ".join(v for v in (model,capacity,color) if v), "UNKNOWN",
                          prices, product_url, categories=["Apple",category], provider="apple", metadata=metadata)
        if sku in records and records[sku] != product:
            raise AutoGrabError("APPLE_CATALOG_VARIANT_AMBIGUOUS")
        records[sku] = product
    if not records:
        raise AutoGrabError("APPLE_CATALOG_SKUS_UNVERIFIED")
    return list(records.values())


class AppleCatalog:
    def __init__(self, region, *, transport=None, progress=None):
        if region not in REGIONS:
            raise AutoGrabError("APPLE_REGION_NOT_CONFIGURED")
        self.region, self.base = region, REGIONS[region]
        self.transport, self.progress = transport or public_html, progress or (lambda _message: None)
        self.pages = {}

    def page(self, url):
        if url not in self.pages:
            self.progress(url)
            self.pages[url] = self.transport(url)
        return self.pages[url]

    def categories(self):
        page, result = PublicPage(self.page(self.base + "/store")), {}
        prefix = urlsplit(self.base).path
        for link in page.links:
            url = urljoin(self.base+"/", link.get("href", "")); parts = urlsplit(url)
            match = re.fullmatch(re.escape(prefix)+r"/shop/buy-([a-z0-9-]+)/?",parts.path)
            if _official_url(url, self.region) and match:
                result[match[1]] = url.rstrip("/")
        if not result:
            raise AutoGrabError("APPLE_CATALOG_CATEGORIES_UNVERIFIED")
        return result

    def models(self, category):
        categories = self.categories()
        if category not in categories:
            raise AutoGrabError("APPLE_CATALOG_CATEGORY_UNKNOWN")
        url = categories[category]
        page, result = PublicPage(self.page(url)), {}
        path = urlsplit(url).path
        for link in page.links:
            target = urljoin(url, link.get("href", "")); parts = urlsplit(target)
            match = re.fullmatch(re.escape(path)+r"/([a-z0-9-]+)/?",parts.path)
            if _official_url(target, self.region) and match:
                result[match[1]] = target.rstrip("/")
        if not result:
            raise AutoGrabError("APPLE_CATALOG_MODELS_UNVERIFIED")
        return result

    def model_products(self, url):
        return parse_purchase_page(self.page(url), url, self.region)

    def refresh(self, categories):
        self.pages = {}
        records = {}
        for category in categories:
            models = self.models(category)
            if len(models) > 32:
                raise AutoGrabError("APPLE_CATALOG_LIMIT_REACHED")
            for url in models.values():
                for product in self.model_products(url):
                    if product.product_id in records and records[product.product_id] != product:
                        raise AutoGrabError("APPLE_CATALOG_IDENTITY_CONFLICT")
                    records[product.product_id] = product
                if self.transport is public_html:
                    time.sleep(0.5)
        if not records:
            raise AutoGrabError("APPLE_CATALOG_EMPTY")
        return list(records.values())

    def stores(self):
        page = PublicPage(self.page(self.base + "/retail/storelist/"))
        ids = {a.get("data-store-number"):plain(a["text"]) for a in page.links if re.fullmatch(r"R[0-9]{3,5}",a.get("data-store-number", ""))}
        found = {}
        def walk(value):
            if isinstance(value,dict):
                identity = value.get("id")
                if isinstance(identity,str) and identity in ids and isinstance(value.get("address"),dict):
                    found[identity] = {"store_id":identity, "name":ids[identity], "city":plain(value["address"].get("city"))}
                for child in value.values():
                    walk(child)
            elif isinstance(value,list):
                for child in value:
                    walk(child)
        for script in page.scripts:
            if script.get("id") == "__NEXT_DATA__":
                try: walk(json.loads(script["text"]))
                except ValueError: pass
        if not ids or set(found) != set(ids):
            raise AutoGrabError("APPLE_STORES_UNVERIFIED")
        return sorted(found.values(),key=lambda item:(item["city"],item["name"]))


def merge_observations(current, previous):
    """Share the existing product ledger without catalog/stock overwriting each other."""
    prior = {p.product_id:p for p in previous if p.provider == "apple"}
    scopes = {p.metadata.get("catalog_scope") for p in prior.values()
              if p.metadata.get("catalog_discovered") is True and p.metadata.get("catalog_complete") is True}
    result = {}
    for product in current:
        same_batch = product.product_id in result
        old = result.get(product.product_id) or prior.get(product.product_id)
        meta = dict(product.metadata)
        if meta.get("catalog_discovered") is True:
            first = meta.get("catalog_scope") not in scopes
            meta.update(catalog_baseline=first, catalog_new_sku=not first and product.product_id not in prior)
            if old:
                meta = {**old.metadata, **meta}
            if old and not meta.get("inventory"):
                meta["configured_target"] = old.metadata.get("configured_target",False)
                meta["inventory"] = [{**v,"fresh":False} for v in old.metadata.get("inventory",[])]
                product = replace(product,locations=old.locations,eligible=old.eligible)
        elif old and old.metadata.get("catalog_discovered") is True:
            meta = {**old.metadata, **meta, "catalog_discovered":True,
                "catalog_new_sku":old.metadata.get("catalog_new_sku",False) if same_batch else False,
                "catalog_baseline":old.metadata.get("catalog_baseline",False) if same_batch else False}
            product = replace(product,name=old.name,prices=old.prices,product_url=old.product_url,categories=old.categories)
        result[product.product_id] = replace(product,metadata=meta)
    return list(result.values())
