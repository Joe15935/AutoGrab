"""Classify observed changes, never marketing words or price levels.

The result annotates an existing catalogue event; it is not an order permit.
Only a fresh official AVAILABLE observation can be an execution candidate.
"""
from __future__ import annotations

def signals(product: dict) -> list[str]:
    text = " ".join([product.get("name", ""), *product.get("categories", [])]).upper()
    return [word for word in ("SPECIAL", "PROMO", "ANNUAL", "LIMITED", "BLACK FRIDAY") if word in text]


def annual(product: dict) -> set[str]:
    return {str(p.get("period", "")).lower() for p in product.get("prices", [])
            if p.get("available") is not False and str(p.get("period", "")).lower() in {"annually", "biennially", "triennially"}}


def stable(product: dict) -> dict:
    """Exclude observation timestamps, not the observed inventory dimensions."""
    def clean(value, path=()):
        if isinstance(value, dict):
            return {k: clean(v, (*path, k)) for k, v in value.items()
                    if k not in {"observed_at", "last_checked", "request_status", "inventory_status", "fresh", "age_seconds", "catalog_baseline", "catalog_new_sku"}
                    and not (k == "status" and "inventory" in path)}
        if isinstance(value, list):
            result = [clean(v, path) for v in value]
            if path == ("metadata", "inventory"):
                result.sort(key=lambda v: str(tuple(v.get(k) for k in ("sku", "region", "mode", "store_id"))) if isinstance(v, dict) else str(v))
            return result
        return value
    result = clean(product)
    result.setdefault("metadata", {})
    return result


def classify_event(previous: dict | None, current: dict, *, new_groups=(), new_url=False) -> dict:
    """Called only after baseline exists, using the *fresh*, unmerged snapshot."""
    types = []
    apple = current.get("provider") == "apple"
    # An explicit monitoring target is not evidence of a newly released SKU.
    # Each newly selected catalog scope also initializes silently. A full later
    # official catalog scan is required to establish a genuinely new SKU.
    meta = current.get("metadata", {})
    configured_baseline = apple and (meta.get("catalog_baseline") is True
        or (previous is None and meta.get("catalog_new_sku") is not True))
    available = current.get("availability") == "AVAILABLE"
    if apple:
        available = available and any(p.get("fresh") is True and p.get("availability") == "AVAILABLE"
            for p in current.get("metadata", {}).get("inventory", []) if isinstance(p, dict))
    if previous is None:
        if not configured_baseline:
            types.append("NEW_SKU" if apple else "NEW_PRODUCT")
            if annual(current):
                types.append("NEW_ANNUAL_SKU")
    else:
        if not apple and previous.get("availability") == "SOLD_OUT" and available:
            types.append("RESTOCK")
        if annual(current) - annual(previous):
            types.append("NEW_ANNUAL_SKU")
        before, after = previous.get("metadata", {}), current.get("metadata", {})
        if before.get("public") is False and after.get("public") is True:
            types.append("HIDDEN_TO_PUBLIC")
        if after.get("campaign_items") and set(after["campaign_items"]) - set(before.get("campaign_items", [])):
            types.append("ACTIVITY_PAGE_CHANGE")
        if apple:
            if before.get("order_state") in {"CLOSED", "UNAVAILABLE"} and after.get("order_state") in {"PREORDER", "OPEN"}:
                types.append("PREORDER_OPEN" if after["order_state"] == "PREORDER" else "ORDER_OPEN")
            def inventory(meta):
                return {(p.get("sku"), p.get("region"), p.get("mode"), p.get("store_id")): p
                        for p in meta.get("inventory", []) if isinstance(p, dict)}
            old = inventory(before)
            for identity, item in inventory(after).items():
                # Unknown, newly configured locations and stale observations are not restocks.
                prior = old.get(identity, {})
                if (item.get("availability") == "AVAILABLE" and item.get("fresh") is True
                        and prior.get("availability") == "SOLD_OUT"):
                    types.append("PICKUP_AVAILABLE" if item.get("mode") == "pickup" else "DELIVERY_AVAILABLE")
    if new_groups and not configured_baseline:
        types.append("NEW_PRODUCT_GROUP")
    if new_url and not configured_baseline:
        types.append("NEW_ORDER_URL")
    return {"opportunities": list(dict.fromkeys(types)), "signals": signals(current),
            "official_observation": True, "observed_availability": current.get("availability", "UNKNOWN"),
            "execution_candidate": bool(types and available),
            "regular": not bool(types)}
