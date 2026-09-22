"""Strict bounded Native Messaging contract, containing public metadata only."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import struct
from typing import BinaryIO
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID, uuid4

MAX_FRAME_BYTES = 65536
COMMANDS = frozenset({"PING", "GET_STATUS", "DISARM", "OPEN_PRODUCT", "START_DRY_RUN",
                      "START_CHECKOUT", "RESUME_INTENT", "CANCEL_INTENT",
                      "ORDER_PRECHECK", "SUBMIT_ORDER", "RECONCILE_ORDER"})
EVENTS = frozenset({"EDGE_READY", "PAGE_OPENED", "PRODUCT_VERIFIED", "CART_READY",
                   "CHECKOUT_READY", "ORDER_CREATED", "INVOICE_FOUND", "PAYMENT_READY",
                   "LOGIN_REQUIRED", "HUMAN_CHALLENGE_REQUIRED", "SOLD_OUT", "SITE_CHANGED", "FAILED", "ORDER_OBSERVATION"})
CONTROLS = frozenset({"PING", "GET_STATUS", "DISARM"})
ENVELOPE = frozenset({"version", "type", "message_id", "command_id", "intent_id",
                      "provider", "product_id", "timestamp", "payload"})
EVENT_FIELDS = frozenset({"version", "tab_id", "url", "stage", "code", "login", "challenge",
                         "cart_id", "resume_from", "mutation_uncertain"})
PROVIDERS = frozenset({"bandwagon", "dmit", "vmiss", "vps", "apple"})
PERIODS = frozenset({"monthly", "quarterly", "semiannually", "annually", "biennially", "triennially", "one_time", "unknown"})
CURRENCIES = frozenset({"USD", "EUR", "CNY", "HKD", "CAD", "GBP"})
APPLE_REGIONS = {"us": ("www.apple.com", ""), "cn": ("www.apple.com.cn", ""),
                 "hk": ("www.apple.com", "/hk-zh"),
                 **{region: ("www.apple.com", "/" + region) for region in ("tw", "jp", "sg", "au", "my")}}


class ProtocolError(ValueError):
    """Fixed codes only; never expose rejected untrusted message content."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def timestamp(value: str) -> datetime:
    if (not isinstance(value, str) or len(value) > 40
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)", value)):
        raise ProtocolError("TIMESTAMP_INVALID")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise ProtocolError("TIMESTAMP_INVALID") from None


def product_identity(provider, value):
    if provider not in PROVIDERS or not isinstance(value, str):
        raise ProtocolError("IDENTITY_INVALID")
    pattern = (r"[a-z]{2}:[A-Z0-9]{1,30}/A" if provider == "apple" else
               r"[a-z0-9][a-z0-9-]{0,79}/[a-z0-9][a-z0-9-]{0,79}" if provider == "vmiss" else
               r"[1-9][0-9]{0,39}")
    if not re.fullmatch(pattern, value):
        raise ProtocolError("IDENTITY_INVALID")
    return value


def _query(parts):
    if parts.query and any("=" not in segment for segment in parts.query.split("&")):
        raise ValueError()
    query = parse_qs(parts.query, keep_blank_values=True)
    if any(len(v) != 1 for v in query.values()):
        raise ValueError()
    return {key: values[0] for key, values in query.items()}


def _whmcs_product(parts):
    query = _query(parts)
    if re.fullmatch(r"/store(?:/[a-z0-9][a-z0-9-]{0,79}){0,2}/?", parts.path):
        return not query
    if parts.path != "/cart.php":
        return False
    if not query:
        return True
    numeric = lambda value: bool(re.fullmatch(r"[1-9][0-9]{0,39}", value))
    if "a" in query:
        if query.get("a") != "add" or not numeric(query.get("pid", "")):
            return False
        keys = {"a", "pid", "billingcycle", "currency", "language"}
    else:
        keys = {"gid", "currency", "language"}
    for key, value in query.items():
        if re.fullmatch(r"configoption\[[0-9]+\]", key) and "a" in query:
            if not numeric(value):
                return False
        elif key not in keys:
            return False
        elif key in {"gid", "currency"} and not numeric(value):
            return False
        elif key == "language" and value != "english":
            return False
        elif key == "billingcycle" and value not in PERIODS - {"unknown", "one_time"}:
            return False
    return True


def public_url(value, *, product=False, provider="bandwagon", product_id=None):
    if (not isinstance(value, str) or len(value) > 2048 or not value.isascii()
            or provider not in PROVIDERS or not value.startswith("https://")
            or "#" in value or value.endswith("?")
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ProtocolError("URL_INVALID")
    try:
        parts = urlsplit(value)
        decoded = unquote(parts.path, errors="strict")
        if (parts.scheme != "https" or parts.fragment
                or ".." in decoded.split("/") or "\\" in decoded or "%" in decoded
                or any(ord(c) < 32 or ord(c) == 127 for c in decoded)):
            raise ValueError()
        if provider == "bandwagon":
            if parts.netloc != "bandwagonhost.com":
                raise ValueError()
            if product:
                valid = bool(re.fullmatch(r"/order/ecommerce(?:/[^/?#]+/[^/?#]+)?/?", parts.path)) and not parts.query
            else:
                valid = (parts.path in {"/", "/clientarea.php", "/index.php", "/cart.php"}
                         or bool(re.fullmatch(r"/order/ecommerce(?:/[^/?#]+/[^/?#]+)?/?", parts.path)))
                valid = valid and (not parts.query or (parts.path == "/cart.php" and parts.query in
                                        {"a=view", "a=confproduct", "a=checkout"}))
        elif provider in {"dmit", "vmiss"}:
            if parts.netloc != {"dmit": "www.dmit.io", "vmiss": "app.vmiss.com"}[provider]:
                raise ValueError()
            valid = _whmcs_product(parts) if product else (
                parts.path in {"/", "/clientarea.php", "/index.php", "/cart.php"} or
                bool(re.fullmatch(r"/store(?:/[a-z0-9][a-z0-9-]{0,79}){0,2}/?", parts.path)))
            if not product:
                valid = valid and (not parts.query or parts.path == "/cart.php" and parts.query in {"a=view", "a=confproduct", "a=checkout"})
            if product and product_id:
                query = _query(parts)
                if "pid" in query and query["pid"] != product_id:
                    valid = False
                if provider == "vmiss":
                    valid = valid and parts.path.rstrip("/") in {"/store/" + product_id, "/store/" + product_id.split("/")[0]}
        elif provider == "vps":
            if parts.netloc == "v.ps":
                valid = bool(re.fullmatch(r"/products/[a-z0-9-]{1,80}/?", parts.path)) and not parts.query
                if not product and parts.path == "/" and not parts.query:
                    valid = True
            elif parts.netloc == "vps.hosting":
                query = _query(parts)
                valid = parts.path == "/" and query == {"cmd": "cart", "action": "add", "id": query.get("id")}
                valid = valid and bool(re.fullmatch(r"[1-9][0-9]{0,39}", query.get("id", "")))
                if product_id and query.get("id") != product_id:
                    valid = False
                if not product:
                    valid = (parts.path == "/" and parts.query in {"", "cmd=cart", "cmd=cart&action=view", "cmd=cart&action=checkout"}) or (not query and bool(re.fullmatch(r"/cart/[a-z0-9-]{1,80}/?", parts.path)))
            else:
                valid = False
        else:
            # No account, bag ID, checkout token, or query crosses the bridge.
            valid = False
            for region, (host, prefix) in APPLE_REGIONS.items():
                if parts.netloc != host or not decoded.startswith(prefix + "/shop/") or parts.query:
                    continue
                path = re.fullmatch(re.escape(prefix) + r"/shop/(?:product/([A-Za-z0-9]{1,30}/[Aa])(?:/[a-zA-Z0-9-]+)?|buy-[a-z0-9-]+(?:/[a-zA-Z0-9-]+)*)/?", decoded, re.I)
                if path:
                    matches = True
                    if product and product_id:
                        wanted_region, sku = product_id.split(":", 1)
                        matches = wanted_region == region and (path[1] is None or path[1].upper() == sku)
                        if path[1] is None and decoded.rstrip("/").lower().endswith("/a"):
                            matches = matches and "/".join(decoded.rstrip("/").split("/")[-2:]).upper() == sku
                    valid = valid or matches
                if not product and decoded in {prefix + "/shop/bag", prefix + "/shop/bag/"}:
                    valid = True
            if not product:
                valid = valid or (parts.netloc in {"www.apple.com", "www.apple.com.cn"}
                                  and parts.path == "/" and not parts.query)
        if not valid:
            raise ValueError()
    except (ValueError, TypeError):
        raise ProtocolError("URL_INVALID") from None
    return value


def _validate(message, *, direction=None, fresh=True):
    if type(message) is not dict or set(message) != ENVELOPE:
        raise ProtocolError("ENVELOPE_INVALID")
    if type(message["version"]) is not int or message["version"] != 1 or message["provider"] not in PROVIDERS:
        raise ProtocolError("VERSION_OR_PROVIDER_INVALID")
    kind = message["type"]
    allowed = COMMANDS if direction == "command" else EVENTS if direction == "event" else COMMANDS | EVENTS
    if not isinstance(kind, str) or kind not in allowed:
        raise ProtocolError("MESSAGE_TYPE_INVALID")
    if not _uuid(message["message_id"]) or not _uuid(message["command_id"]):
        raise ProtocolError("MESSAGE_ID_INVALID")
    intent, product = message["intent_id"], message["product_id"]
    if (intent is None) != (product is None):
        raise ProtocolError("IDENTITY_INVALID")
    if intent is not None:
        if not _uuid(intent):
            raise ProtocolError("IDENTITY_INVALID")
        product_identity(message["provider"], product)
    if kind in CONTROLS | {"EDGE_READY"} and intent is not None:
        raise ProtocolError("CONTROL_IDENTITY_INVALID")
    if kind in CONTROLS | {"EDGE_READY"} and message["provider"] != "bandwagon":
        raise ProtocolError("CONTROL_IDENTITY_INVALID")
    if kind in COMMANDS - CONTROLS and intent is None:
        raise ProtocolError("IDENTITY_REQUIRED")
    if kind in EVENTS - {"EDGE_READY", "PAGE_OPENED"} and intent is None:
        raise ProtocolError("IDENTITY_REQUIRED")
    sent = timestamp(message["timestamp"])
    if fresh and abs((datetime.now(timezone.utc) - sent).total_seconds()) > 120:
        raise ProtocolError("MESSAGE_STALE")
    payload = message["payload"]
    if type(payload) is not dict:
        raise ProtocolError("PAYLOAD_INVALID")
    if kind in {"ORDER_PRECHECK", "SUBMIT_ORDER", "RECONCILE_ORDER"}:
        from .order_protocol import validate_order_command
        return validate_order_command(message)
    if kind == "ORDER_OBSERVATION":
        from .order_protocol import validate_observation
        validate_observation(payload, message["provider"])
        return message
    if kind in COMMANDS:
        if kind in CONTROLS | {"CANCEL_INTENT"}:
            if payload:
                raise ProtocolError("PAYLOAD_INVALID")
        else:
            if set(payload) != {"product", "mode"} or payload["mode"] != "DRY_RUN":
                raise ProtocolError("DRY_RUN_REQUIRED")
            item = payload["product"]
            if type(item) is not dict or set(item) != {"name", "url", "period", "cents", "currency"}:
                raise ProtocolError("PRODUCT_INVALID")
            if (not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 200
                    or any(ord(c) < 32 or ord(c) == 127 for c in item["name"])
                    or item["period"] not in ( {"annually", "biennially"} if message["provider"] == "bandwagon" else PERIODS)
                    or not (type(item["cents"]) is int and 0 < item["cents"] < 10**12
                            or message["provider"] != "bandwagon" and item["cents"] is None)
                    or item["currency"] not in ({"USD"} if message["provider"] == "bandwagon" else CURRENCIES | {None})):
                raise ProtocolError("PRODUCT_INVALID")
            if kind != "OPEN_PRODUCT" and (item["cents"] is None or item["currency"] is None or item["period"] == "unknown"):
                raise ProtocolError("VERIFIED_PRICE_REQUIRED")
            public_url(item["url"], product=True, provider=message["provider"], product_id=product)
            if kind == "OPEN_PRODUCT" and (item["cents"] is None or item["currency"] is None or item["period"] == "unknown"):
                query = parse_qs(urlsplit(item["url"]).query)
                if query.get("a") == ["add"] or query.get("action") == ["add"]:
                    raise ProtocolError("READ_ONLY_PRODUCT_URL_REQUIRED")
    else:
        if set(payload) - EVENT_FIELDS:
            raise ProtocolError("PAYLOAD_INVALID")
        for field in ("stage", "code", "resume_from"):
            if field in payload and (not isinstance(payload[field], str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", payload[field])):
                raise ProtocolError("STATUS_INVALID")
        for field, values in (("login", {"VALID", "REQUIRED", "UNKNOWN"}), ("challenge", {"NONE", "REQUIRED", "UNKNOWN"})):
            if field in payload and payload[field] not in values:
                raise ProtocolError("STATUS_INVALID")
        if "version" in payload and (not isinstance(payload["version"], str) or not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}", payload["version"])):
            raise ProtocolError("EXTENSION_VERSION_INVALID")
        if kind == "EDGE_READY" and "version" not in payload:
            raise ProtocolError("EXTENSION_VERSION_REQUIRED")
        if "tab_id" in payload and (type(payload["tab_id"]) is not int or not 0 <= payload["tab_id"] < 2**31):
            raise ProtocolError("TAB_ID_INVALID")
        if "cart_id" in payload and (not isinstance(payload["cart_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", payload["cart_id"])):
            raise ProtocolError("CART_ID_INVALID")
        if "mutation_uncertain" in payload and type(payload["mutation_uncertain"]) is not bool:
            raise ProtocolError("UNCERTAINTY_INVALID")
        if "url" in payload:
            public_url(payload["url"], provider=message["provider"])
    return message


def validate(message, *, direction=None, fresh=True):
    try:
        return _validate(message, direction=direction, fresh=fresh)
    except (TypeError, KeyError, OverflowError, RecursionError):
        raise ProtocolError("SCHEMA_INVALID") from None


def make_message(kind, *, intent_id=None, product_id=None, payload=None, command_id=None, provider="bandwagon"):
    return validate(dict(version=1, type=kind, message_id=str(uuid4()), command_id=command_id or str(uuid4()),
                         intent_id=intent_id, provider=provider, product_id=product_id,
                         timestamp=now(), payload=payload or {}))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(_value):
    raise ProtocolError("NONFINITE_JSON")


def decode(data: bytes, *, direction=None):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FRAME_BYTES:
        raise ProtocolError("FRAME_SIZE_INVALID")
    try:
        value = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
                           parse_constant=_nonfinite)
    except ProtocolError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise ProtocolError("JSON_INVALID") from None
    return validate(value, direction=direction)


def encode(message) -> bytes:
    validate(message)
    try:
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ProtocolError("JSON_INVALID") from None
    if not 0 < len(raw) <= MAX_FRAME_BYTES:
        raise ProtocolError("FRAME_SIZE_INVALID")
    return struct.pack("=I", len(raw)) + raw


def read_message(stream: BinaryIO, *, direction=None):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ProtocolError("FRAME_TRUNCATED")
    size = struct.unpack("=I", header)[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise ProtocolError("FRAME_SIZE_INVALID")
    raw = bytearray()
    while len(raw) < size:
        chunk = stream.read(size - len(raw))
        if not chunk:
            raise ProtocolError("FRAME_TRUNCATED")
        raw.extend(chunk)
    return decode(bytes(raw), direction=direction)


def validate_origin(extension_id: str, origin: str):
    if not isinstance(extension_id, str) or not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ProtocolError("EXTENSION_ID_INVALID")
    if origin != f"chrome-extension://{extension_id}/":
        raise ProtocolError("ORIGIN_REJECTED")
    return True
