"""Additive Phase 2 read/cart inspection policy; order/payment POST stays denied.

Authenticated no-charge order semantics are still unverified. No configuration
flag can enable the final checkout POST in this build.
"""
import re
from .safety import SafetyPolicy, HOST, _split, _query


class CheckoutPolicy(SafetyPolicy):
    def __init__(self):
        super().__init__()
        self.account_reads = False
        self.checkout_reads = False
        self._configuration_post = None

    def allow_configuration_post(self, url):
        parsed = _split(url)
        query = _query(parsed.query) if parsed else None
        if (parsed is None or parsed.scheme != "https" or parsed.netloc != HOST
                or parsed.path != "/cart.php" or parsed.fragment or not query
                or set(query) != {"a", "i"} or query.get("a") != ["confproduct"]
                or len(query["i"]) != 1 or not re.fullmatch(r"[0-9]+", query["i"][0])):
            raise ValueError("Unverified configuration endpoint")
        if self._configuration_post is not None:
            raise ValueError("Configuration ticket already exists")
        self._configuration_post = url

    def permits(self, url, method, resource_type):
        parsed = _split(url)
        if parsed and parsed.scheme == "https" and parsed.netloc == HOST and not parsed.fragment:
            query = _query(parsed.query)
            if method == "POST" and resource_type == "document" and url == self._configuration_post:
                self._configuration_post = None
                return True
            if method == "GET" and resource_type == "document":
                if self.checkout_reads and parsed.path == "/cart.php" and query == {"a": ["checkout"]}:
                    return True
                if self.account_reads:
                    if parsed.path in {"/clientarea.php", "/login.php"} and not parsed.query:
                        return True
                    if parsed.path == "/clientarea.php" and query in ({"action": ["invoices"]}, {"action": ["products"]}):
                        return True
                    if parsed.path == "/viewinvoice.php" and query and set(query) == {"id"} and len(query["id"]) == 1 and re.fullmatch(r"[1-9][0-9]*", query["id"][0]):
                        return True
        return super().permits(url, method, resource_type)
