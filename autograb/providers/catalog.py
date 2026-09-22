"""Parse the public BandwagonHost catalog without guessing stock or prices.

The parser is deliberately atomic: one malformed product invalidates the whole
snapshot. A truncated or changed feed must never look like products disappeared.
Grouping URLs identify the public catalog page only; they are not order actions.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from autograb.core.models import Product


CATALOG_URL = "https://bandwagonhost.com/order/basic"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z", re.ASCII)
_PRODUCT_ID = re.compile(r"[1-9][0-9]*\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_PROMO = re.compile(
    r"\b(?:promo(?:tion(?:al)?)?|special|limited|discount(?:ed)?|sale|deal|offer)s?\b"
    r"|促销|限量|特价|限时|优惠",
    re.IGNORECASE,
)
_ANNUAL_PERIODS = {"annually", "annual", "yearly", "year", "1 year", "12 months"}


class CatalogError(ValueError):
    """The response cannot be treated as a complete, trustworthy snapshot."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{field} must be a non-empty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CatalogError(f"{field} contains control characters")
    return " ".join(value.split())


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CatalogError(f"{field} is not a safe catalog identifier")
    return value


def _product_id(value: Any, field: str = "product.id") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise CatalogError(f"{field} must be a positive integer ID")
    result = str(value)
    if not _PRODUCT_ID.fullmatch(result):
        raise CatalogError(f"{field} must be a positive integer ID")
    return result


def _list(value: Any, field: str) -> list:
    if not isinstance(value, list):
        raise CatalogError(f"{field} must be an array")
    return value


def _object(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise CatalogError(f"{field} must be an object")
    return value


def _tier_ids(value: Any, field: str) -> list[str]:
    result = [_identifier(item, field) for item in _list(value, field)]
    if len(result) != len(set(result)):
        raise CatalogError(f"{field} contains duplicate identifiers")
    return result


def _prices(value: Any) -> list[dict[str, Any]]:
    result = []
    for entry in _list(value, "product.prices"):
        entry = _object(entry, "product.prices[]")
        cents = entry.get("cents")
        if cents is not None and (isinstance(cents, bool) or not isinstance(cents, int)):
            raise CatalogError("price.cents must be an integer or null")
        currency = entry.get("currency")
        if currency is not None:
            currency = _text(currency, "price.currency").upper()
            if not _CURRENCY.fullmatch(currency):
                raise CatalogError("price.currency must be a three-letter currency code")
        period = entry.get("period")
        if period is not None:
            period = _text(period, "price.period")
        # Negative amounts are retained as unavailable provider values. Missing
        # values remain unknown, and neither can silently become a free price.
        result.append(
            {
                "cents": cents,
                "currency": currency,
                "period": period,
                "available": None if cents is None else cents >= 0,
            }
        )
    return result


def parse_catalog(payload: dict) -> list[Product]:
    """Return all products, or raise :class:`CatalogError` for an invalid feed.

    Eligibility is based only on promotional wording or an annual billing
    period. Stock, hardware specifications, and price amount do not filter the
    catalog. The order URL deliberately remains unset for browser verification.
    """
    payload = _object(payload, "catalog")
    if payload.get("error") or payload.get("errors") or payload.get("success") is False:
        raise CatalogError("catalog reports an error")
    products = _list(payload.get("products"), "catalog.products")
    if not products:
        raise CatalogError("catalog.products must not be empty")

    tiers: dict[str, str] = {}
    for entry in _list(payload.get("tiers"), "catalog.tiers"):
        entry = _object(entry, "catalog.tiers[]")
        tier_id = _identifier(entry.get("id"), "tier.id")
        if tier_id in tiers:
            raise CatalogError("catalog contains duplicate tier IDs")
        tiers[tier_id] = _text(entry.get("name"), "tier.name")

    datacenters: list[tuple[str, str, list[str]]] = []
    seen_datacenters: set[str] = set()
    for location in _list(payload.get("locations"), "catalog.locations"):
        location = _object(location, "catalog.locations[]")
        city = _text(location.get("city"), "location.city")
        if city in {".", ".."} or any(character in city for character in "/\\%?#"):
            raise CatalogError("location.city is not a safe route segment")
        for dc in _list(location.get("datacenters"), "location.datacenters"):
            dc = _object(dc, "location.datacenters[]")
            dc_id = _identifier(dc.get("id"), "datacenter.id")
            if dc_id in seen_datacenters:
                raise CatalogError("catalog contains duplicate datacenter IDs")
            seen_datacenters.add(dc_id)
            dc_tiers = _tier_ids(dc.get("tiers"), "datacenter.tiers")
            datacenters.append((city, dc_id, dc_tiers))

    result = []
    seen_products: set[str] = set()
    for entry in products:
        entry = _object(entry, "catalog.products[]")
        product_id = _product_id(entry.get("id"))
        if product_id in seen_products:
            raise CatalogError("catalog contains duplicate product IDs")
        seen_products.add(product_id)
        name = _text(entry.get("name"), "product.name")
        stock = entry.get("outOfStock")
        if stock is not None and not isinstance(stock, bool):
            raise CatalogError("product.outOfStock must be a boolean or null")
        availability = "UNKNOWN" if stock is None else "SOLD_OUT" if stock else "AVAILABLE"
        prices = _prices(entry.get("prices", []))
        product_tiers = _tier_ids(entry.get("tiers", []), "product.tiers")
        categories = [tiers.get(tier_id, tier_id) for tier_id in product_tiers]
        product_datacenters = _object(entry.get("datacenters", {}), "product.datacenters")
        for dc_id, option_id in product_datacenters.items():
            _identifier(dc_id, "product.datacenters key")
            _product_id(option_id, "product.datacenters option ID")

        product_url = CATALOG_URL
        product_locations: list[str] = []
        resolved = False
        for tier_id in product_tiers:
            for city, dc_id, dc_tiers in datacenters:
                if tier_id not in tiers or dc_id not in product_datacenters or tier_id not in dc_tiers:
                    continue
                if city not in product_locations:
                    product_locations.append(city)
                if not resolved:
                    product_url = (
                        f"https://bandwagonhost.com/order/{tier_id}/{quote(city, safe='')}/{dc_id}"
                    )
                    resolved = True

        eligible = bool(_PROMO.search(" ".join([name, *categories, *product_tiers]))) or any(
            (price["period"] or "").casefold() in _ANNUAL_PERIODS for price in prices
        )
        result.append(
            Product(
                product_id=product_id,
                name=name,
                availability=availability,
                prices=prices,
                product_url=product_url,
                categories=categories,
                locations=product_locations,
                eligible=eligible,
            )
        )
    return result
