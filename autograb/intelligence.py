"""Untrusted NodeSeek RSS signals, isolated from purchase and event execution.

Only the fixed public RSS URL is fetched. Official URLs are candidates for later
official verification; neither keywords nor forum claims establish inventory.
No database, event publisher, order, browser or provider actions are imported.
"""
from html import unescape
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

from autograb.core.errors import AutoGrabError

SOURCE = "https://rss.nodeseek.com/"
MAX_BYTES = 1_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _candidate(raw):
    try:
        url = urlsplit(raw)
    except ValueError:
        return None
    if url.scheme != "https" or url.query or url.fragment or re.search(r"[\s\x00-\x1f]", raw):
        return None
    rules = {
        "bandwagonhost.com": ("bandwagon", r"/order/ecommerce(?:/[^/?#]+/[^/?#]+)?/?"),
        "www.dmit.io": ("dmit", r"/(?:cart\.php)?"),
        "app.vmiss.com": ("vmiss", r"/store(?:/[a-z0-9][a-z0-9-]{0,79}){0,2}/?"),
        "v.ps": ("vps", r"/(?:vps/|products/[a-z0-9-]+-kvm-vps/)?"),
        "www.apple.com": ("apple", r"/(?:[a-z]{2}/)?shop/refurbished(?:/[a-z0-9-]+)*/*"),
        "www.apple.com.cn": ("apple", r"/shop/refurbished(?:/[a-z0-9-]+)*/*"),
    }
    rule = rules.get(url.netloc)
    return {"provider": rule[0], "url": raw, "official_verification_required": True} if rule and re.fullmatch(rule[1], url.path) else None


def parse_nodeseek_rss(data):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise AutoGrabError("INTELLIGENCE_INVALID")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise AutoGrabError("INTELLIGENCE_INVALID") from None
    if root.tag != "rss" or len(root.findall("channel")) != 1:
        raise AutoGrabError("INTELLIGENCE_INVALID")
    items = root.findall("./channel/item")
    if len(items) > 100:
        raise AutoGrabError("INTELLIGENCE_LIMIT_REACHED")
    signals, seen = [], set()
    for item in items:
        link = item.findtext("link") or ""
        if not re.fullmatch(r"https://www\.nodeseek\.com/post-[1-9][0-9]*-[1-9][0-9]*", link) or link in seen:
            continue
        seen.add(link)
        title = " ".join((item.findtext("title") or "").split())[:500]
        body = unescape((item.findtext("description") or "") + " " + title)
        candidates = {}
        for raw in re.findall(r"https://[^\s<>\"']+", body):
            candidate = _candidate(raw.rstrip(".,);]"))
            if candidate:
                candidates[candidate["url"]] = candidate
        signals.append({"source": "nodeseek", "kind": "FORUM_INTELLIGENCE", "authority": "UNVERIFIED",
                        "source_url": link, "title": title, "official_candidates": list(candidates.values())})
    return signals


def fetch_nodeseek_signals():
    try:
        with build_opener(_NoRedirect).open(Request(SOURCE, headers={
                "User-Agent": "AutoGrab/0.3 (public RSS intelligence; read only)",
                "Accept": "application/rss+xml, application/xml"}), timeout=15) as response:
            if response.status != 200:
                raise AutoGrabError("INTELLIGENCE_UNAVAILABLE")
            return parse_nodeseek_rss(response.read(MAX_BYTES + 1))
    except (OSError, ValueError):
        raise AutoGrabError("INTELLIGENCE_UNAVAILABLE") from None
