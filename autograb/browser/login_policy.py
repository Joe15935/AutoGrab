"""Ordinary challenge resources for the explicit human-only login window.

This does not solve challenges, manufacture tokens, change browser identity or
authorize any cart/order/payment operation. The site still decides admission.
Default monitoring and checkout continue to use their stricter policies.
"""
import re

from .safety import HOST, SafetyPolicy, _query, _safe_path, _split


CHALLENGE_HOST = "challenges.cloudflare.com"
CHALLENGE_PREFIX = "/cdn-cgi/challenge-platform/"
_READ_TYPES = {"document", "script", "xhr", "fetch", "image", "stylesheet", "font", "other"}
_ACCOUNT_PATHS = {"/clientarea.php", "/login.php", "/dologin.php"}
_TURNSTILE_SCRIPT = re.compile(r"/turnstile/v0/(?:g/[0-9a-f]{1,64}/)?api\.js")


class HumanLoginPolicy(SafetyPolicy):
    def __init__(self):
        super().__init__()
        self.login_mode = True

    def permits(self, url, method, resource_type):
        parsed = _split(url)
        if (self.login_mode and parsed is not None and parsed.scheme == "https"
                and parsed.netloc in {HOST, CHALLENGE_HOST} and not parsed.fragment):
            path = _safe_path(parsed.path)
            if path is not None and path == parsed.path:
                # No broad /cdn-cgi, arbitrary host, PHP or generic POST rule.
                if (path.startswith(CHALLENGE_PREFIX) and len(path) > len(CHALLENGE_PREFIX)
                        and ".php" not in path.casefold() and ".phtml" not in path.casefold()):
                    if method in {"GET", "HEAD"} and resource_type in _READ_TYPES:
                        return True
                    if method == "POST" and resource_type in {"xhr", "fetch"}:
                        return True
                # Live console inspection also observed the normal versioned
                # /turnstile/v0/g/<build-id>/api.js script being client-blocked.
                if (parsed.netloc == CHALLENGE_HOST and _TURNSTILE_SCRIPT.fullmatch(path)
                        and method in {"GET", "HEAD"} and resource_type == "script"):
                    return True
                # Only the challenge key actually observed on the login page.
                # Its opaque value remains in the browser and is never logged.
                if (parsed.netloc == HOST and path in _ACCOUNT_PATHS
                        and method in {"GET", "HEAD"} and resource_type == "document"):
                    query = _query(parsed.query)
                    if (query is not None and set(query) == {"__cf_chl_rt_tk"}
                            and len(query["__cf_chl_rt_tk"]) == 1
                            and 0 < len(query["__cf_chl_rt_tk"][0]) <= 2048):
                        return True
        return super().permits(url, method, resource_type)
