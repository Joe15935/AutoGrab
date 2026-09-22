"""Phase 1 network boundary: safe reads and one exact observed cart-config GET.

No form POST, order submission, invoice or payment navigation is permitted.
This is a second layer behind an executor that never clicks Continue/Checkout.
"""

import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

HOST = "bandwagonhost.com"
_PUBLIC_PATHS = {"/", "/index.php", "/vps-hosting.php", "/order/get-data"}
_LOGIN_PATHS = {"/dologin.php", "/login.php"}
_DIAGNOSTIC_PATHS = _PUBLIC_PATHS | _LOGIN_PATHS | {
    "/cart.php", "/clientarea.php", "/viewinvoice.php", "/creditcard.php", "/paypal.php"
}
_STATIC_EXTENSION = re.compile(r"\.(?:js|css|png|svg|jpg|jpeg|gif|webp|ico|woff2?|ttf)$", re.I)


def _split(url):
    """Reject malformed/ambiguous URLs before urllib can silently normalize them."""
    if not isinstance(url, str) or len(url) > 8192:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        # Accessing port also validates malformed or out-of-range port strings.
        _ = parsed.port
        return parsed
    except ValueError:
        return None


def _safe_path(path: str) -> str | None:
    # Encoded separators and repeated encoding are not public catalog paths.
    if re.search(r"%2f|%5c", path, re.I):
        return None
    try:
        decoded = unquote(path, errors="strict")
    except UnicodeError:
        return None
    if (any(ord(char) < 32 or ord(char) == 127 for char in decoded)
            or any(char in decoded for char in "\\%?#") or "//" in decoded
            or any(segment in {".", ".."} for segment in decoded.split("/"))):
        return None
    return decoded


def _catalog_path(path: str) -> bool:
    return bool(re.fullmatch(
        r"/order/(?:basic|ecommerce|ecommerce-sla-elevated|ultra)"
        r"(?:/[^/?#]+(?:/[A-Za-z0-9][A-Za-z0-9_-]*)?)?/?", path
    ))


def _query(value: str) -> dict | None:
    try:
        return parse_qs(value, keep_blank_values=True, strict_parsing=True,
                        max_num_fields=32, errors="strict")
    except (ValueError, UnicodeError):
        return None


def public_url(url: str) -> str:
    """Expose only known public paths on the fixed provider host, without secrets."""
    p = _split(url)
    if p is None or p.scheme != "https" or p.hostname != HOST or p.port not in {None, 443}:
        return "UNAVAILABLE"
    path = _safe_path(p.path)
    if path is None or (path not in _DIAGNOSTIC_PATHS and not _catalog_path(path)):
        return "UNAVAILABLE"
    return urlunsplit(("https", HOST, p.path, "", ""))


def is_observed_add(url: str, product_id: str) -> bool:
    if not isinstance(product_id, str) or not re.fullmatch(r"[1-9][0-9]*", product_id):
        return False
    p = _split(url)
    if (p is None or p.scheme != "https" or p.netloc != HOST
            or p.path != "/cart.php" or p.fragment):
        return False
    q = _query(p.query)
    if (q is None or q.get("a") != ["add"] or q.get("pid") != [product_id]
            or any(len(values) != 1 or not 0 < len(values[0]) < 80 for values in q.values())):
        return False
    for key, values in q.items():
        if key in {"a", "pid"}:
            continue
        if key == "billingcycle":
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", values[0]):
                return False
        elif not (re.fullmatch(r"configoption\[[1-9][0-9]*\]", key)
                  and re.fullmatch(r"[0-9]+", values[0])):
            return False
    return True


class SafetyPolicy:
    def __init__(self):
        self.ticket: str | None = None
        self.blocked: list[dict] = []
        self.login_mode = False

    def arm_config_get(self, url: str, product_id: str):
        if not is_observed_add(url, product_id):
            raise ValueError("Unverified cart entry")
        if self.ticket is not None:
            raise ValueError("A cart entry ticket is already armed")
        self.ticket = url

    def permits(self, url: str, method: str, resource_type: str) -> bool:
        p = _split(url)
        if p is None:
            return False
        if p.scheme in {"data", "about", "blob"}:
            return method in {"GET", "HEAD"}
        if (p.scheme != "https" or not p.hostname or p.username is not None
                or p.password is not None or p.port not in {None, 443}):
            return False
        path = _safe_path(p.path)
        if path is None:
            return False
        # The explicit human login command permits only the ordinary login form.
        if (self.login_mode and p.netloc == HOST and path in _LOGIN_PATHS
                and not p.query and resource_type == "document"):
            return method in {"GET", "HEAD", "POST"}
        if method not in {"GET", "HEAD"}:
            return False
        if p.netloc != HOST:
            return resource_type in {"image", "font", "stylesheet"}
        if path == "/cart.php":
            # Only the exact literal endpoint participates in mutation tickets.
            if p.path != "/cart.php":
                return False
            q = _query(p.query)
            if q is None:
                return False
            if q.get("a") == ["add"]:
                if url == self.ticket and method == "GET" and resource_type == "document":
                    self.ticket = None
                    return True
                return False
            return (q.get("a") == ["confproduct"] and set(q) <= {"a", "i"}
                    and len(q.get("i", [])) == 1
                    and bool(re.fullmatch(r"[0-9]+", q["i"][0]))) or (q == {"a": ["view"]})
        if _catalog_path(path) and not p.query:
            return True
        if path in _PUBLIC_PATHS and not p.query:
            return True
        if self.login_mode and path == "/clientarea.php" and not p.query and resource_type == "document":
            return True
        # PHP PATH_INFO must never qualify merely by ending in an asset suffix.
        if ".php" in path.casefold() or ".phtml" in path.casefold():
            return False
        return (resource_type in {"script", "stylesheet", "image", "font", "other"}
                and bool(_STATIC_EXTENSION.search(path)))

    async def route(self, route):
        r = route.request
        if self.permits(r.url, r.method, r.resource_type):
            await route.continue_()
        else:
            parsed = _split(r.url)
            # Unknown paths may carry private tokens. Keep fixed endpoint names
            # for diagnosis and never retain query strings or URL credentials.
            path = (parsed.path if parsed is not None and parsed.netloc == HOST
                    and parsed.path in _DIAGNOSTIC_PATHS else "[redacted-path]")
            self.blocked.append({"method": r.method, "path": path, "resource_type": r.resource_type})
            del self.blocked[:-100]
            await route.abort("blockedbyclient")
