import asyncio
import json
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.parse import parse_qs, urlsplit

from autograb.browser.safety import is_observed_add
from autograb.core.errors import AutoGrabError
from .catalog import parse_catalog, CatalogError

ENDPOINT = "https://bandwagonhost.com/order/get-data"
CATALOG_PAGE = "https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BandwagonHostProvider:
    provider_name = "bandwagon"

    def __init__(self, browser, log):
        self.browser, self.log = browser, log
        self.routes = {}
        self.source = None
        self.http_attempted = False
        self.last_products = []

    def _http(self):
        from autograb.core.http_metrics import request_started
        request_started()
        self.last_retry_after = None
        request = Request(ENDPOINT, headers={"Accept": "application/json", "User-Agent": "AutoGrab/0.1 (DRY_RUN inventory monitor)", "Referer": CATALOG_PAGE})
        try:
            with build_opener(NoRedirect).open(request, timeout=15) as response:
                return response.status, json.loads(response.read(4_000_001))
        except HTTPError as error:
            self.last_retry_after = error.headers.get("Retry-After")
            return error.code, None
        except Exception:
            return None, None

    async def discover_products(self):
        if self.browser is None:
            # Supported monitoring uses public HTTP only. A security response
            # requires ordinary Edge; never launch authenticated Playwright.
            status, payload = await asyncio.to_thread(self._http)
            if status in {403, 429}:
                raise AutoGrabError("HUMAN_CHALLENGE_REQUIRED" if status == 403 else "RATE_LIMITED")
            if status != 200:
                raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
            try:
                products = parse_catalog(payload)
            except CatalogError:
                raise AutoGrabError("DATA_SOURCE_UNAVAILABLE") from None
            self.source = "PUBLIC_HTTP"
            self.last_products = products
            return products
        if not self.http_attempted:
            self.http_attempted = True
            status, payload = await asyncio.to_thread(self._http)
            try:
                products = parse_catalog(payload) if payload else None
            except CatalogError:
                products = None
            self.routes["A_LIGHTWEIGHT_HTTP"] = {"status": "SUPPORTED" if products else "UNSUPPORTED", "http_status": status}
            self.log.write("DATA_ROUTE", route="A", **self.routes["A_LIGHTWEIGHT_HTTP"])
            # Validate the browser routes once even if A works. B is preferred.
        await self.browser.start()
        page = self.browser.catalog_page
        self.browser.last_page = page
        if page.url == "about:blank":
            await self.browser.navigate(page, CATALOG_PAGE)
            # The public SPA populates after DOMContentLoaded.
            try:
                await page.locator('a[href*="cart.php?a=add"]').first.wait_for(timeout=self.browser.config.timeout_ms)
            except Exception:
                await self.browser.guard_page(page)
        result = None
        try:
            result = await page.evaluate("""async () => {
                const c=new AbortController(); const t=setTimeout(()=>c.abort(),15000);
                try {const r=await fetch('/order/get-data',{credentials:'same-origin',signal:c.signal});
                     return {status:r.status,text:await r.text()};} finally {clearTimeout(t);}
            }""")
            products = parse_catalog(json.loads(result["text"])) if result["status"] == 200 else None
            self.routes["B_BROWSER_FETCH"] = {"status": "SUPPORTED" if products else "UNSUPPORTED", "http_status": result["status"]}
        except Exception:
            products = None
            self.routes["B_BROWSER_FETCH"] = {"status": "UNSUPPORTED"}
        if result and result["status"] == 403:
            self.log.write("DATA_ROUTE", route="B_BROWSER_FETCH", **self.routes["B_BROWSER_FETCH"])
            body = result.get("text", "").casefold()
            code = "CAPTCHA_REQUIRED" if any(term in body for term in ("captcha", "verify you are human", "just a moment")) else "CLOUDFLARE" if "cloudflare" in body else "HTTP_403"
            raise AutoGrabError(code)
        # A fallback DOM snapshot must belong to this observation, not an old tab.
        if not products:
            await self.browser.navigate(page, CATALOG_PAGE)
        # Only inspect actual JSON-bearing DOM nodes; never guess window internals.
        embedded = await page.locator('script[type="application/json"],script#__NEXT_DATA__').all_text_contents()
        dom_products = None
        for item in embedded:
            try:
                dom_products = parse_catalog(json.loads(item))
                break
            except (CatalogError, ValueError):
                continue
        self.routes["C_DOM_JSON"] = {"status": "SUPPORTED" if dom_products else "UNSUPPORTED", "json_nodes": len(embedded)}
        for route in ("B_BROWSER_FETCH", "C_DOM_JSON"):
            self.log.write("DATA_ROUTE", route=route, **self.routes[route])
        if products:
            self.source = "B_BROWSER_FETCH"
        elif dom_products:
            products, self.source = dom_products, "C_DOM_JSON"
        elif self.routes["A_LIGHTWEIGHT_HTTP"]["status"] == "SUPPORTED":
            status, payload = await asyncio.to_thread(self._http)
            if status != 200:
                self.routes["A_LIGHTWEIGHT_HTTP"] = {"status": "UNSUPPORTED", "http_status": status}
                raise AutoGrabError("HTTP_403" if status == 403 else "NETWORK_ERROR")
            try:
                products, self.source = parse_catalog(payload), "A_LIGHTWEIGHT_HTTP"
            except CatalogError:
                raise AutoGrabError("DATA_SOURCE_UNAVAILABLE") from None
        else:
            await self.browser.guard_page(page)
            raise AutoGrabError("DATA_SOURCE_UNAVAILABLE")
        self.last_products = products
        return products

    async def check_product(self, product_id):
        products = await self.discover_products()
        product = next((p for p in products if p.product_id == product_id), None)
        if product is None:
            raise AutoGrabError("PRODUCT_MISSING")
        if product.availability == "SOLD_OUT":
            raise AutoGrabError("OUT_OF_STOCK")
        if product.availability != "AVAILABLE":
            raise AutoGrabError("STOCK_UNKNOWN")
        return product

    async def check_stock(self, product_id):
        return (await self.get_product_details(product_id)).availability

    async def get_product_details(self, product_id):
        products = await self.discover_products()
        for product in products:
            if product.product_id == product_id:
                return product
        raise AutoGrabError("PRODUCT_MISSING")

    async def open_product(self, product):
        await self.browser.navigate(self.browser.page, product.product_url)
        page = self.browser.page
        try:
            await page.locator('a[href*="cart.php?a=add"]').first.wait_for()
        except Exception:
            await self.browser.guard_page(page)
            raise AutoGrabError("SELECTOR_CHANGED") from None
        annual = any(p.get("period", "").casefold() in {"annually", "annual", "yearly"} and p.get("available") is not False for p in product.prices if isinstance(p.get("period"), str))
        radio = page.get_by_role("radio", name="1 Year", exact=True)
        if annual and await radio.count() == 1:
            await radio.check()
            if not await radio.is_checked():
                raise AutoGrabError("SELECTOR_CHANGED")

    async def prepare_cart(self, product):
        from autograb.browser.manager import write_marker
        page = self.browser.page
        marker_path = self.browser.config.root / "profiles/bandwagon/.cart-action.json"
        marker = None
        if marker_path.is_symlink():
            raise AutoGrabError("ACTION_OUTCOME_UNKNOWN")
        if marker_path.exists():
            try:
                marker = json.loads(marker_path.read_text())
            except (ValueError, OSError):
                raise AutoGrabError("ACTION_OUTCOME_UNKNOWN") from None
            if (not isinstance(marker, dict) or marker.get("status") != "VERIFIED"
                    or not isinstance(marker.get("product_id"), str)
                    or not isinstance(marker.get("configuration_url"), str)):
                raise AutoGrabError("ACTION_OUTCOME_UNKNOWN")
        recovered_empty = False
        if marker:
            if marker.get("status") != "VERIFIED":
                raise AutoGrabError("ACTION_OUTCOME_UNKNOWN")
            if marker.get("product_id") != product.product_id:
                if not await self.browser.confirm_empty_cart():
                    raise AutoGrabError("CART_REVIEW_REQUIRED")
                recovered_empty, marker = True, None
                await self.open_product(product)
        links = await page.locator('a[href*="cart.php?a=add"]').evaluate_all("nodes=>nodes.map(e=>({href:e.href,text:e.innerText,visible:!!(e.offsetWidth||e.offsetHeight)}))")
        candidates = [v for v in links if v["visible"] and is_observed_add(v["href"], product.product_id)]
        if len(candidates) != 1:
            raise AutoGrabError("SELECTOR_CHANGED")
        href = candidates[0]["href"]
        expected_billing = parse_qs(urlsplit(href).query).get("billingcycle", [None])[0]
        if marker:
            # Re-read an existing configuration; never repeat the add action.
            await self.browser.navigate(page, marker["configuration_url"])
            try:
                evidence = await self.browser.configuration_evidence(product)
            except AutoGrabError as error:
                if error.code != "SELECTOR_CHANGED":
                    raise
                if not await self.browser.confirm_empty_cart():
                    raise AutoGrabError("CART_REVIEW_REQUIRED") from None
                recovered_empty = True
                await self.open_product(product)
                links = await page.locator('a[href*="cart.php?a=add"]').evaluate_all("nodes=>nodes.map(e=>({href:e.href,text:e.innerText,visible:!!(e.offsetWidth||e.offsetHeight)}))")
                candidates = [v for v in links if v["visible"] and is_observed_add(v["href"], product.product_id)]
                if len(candidates) != 1:
                    raise AutoGrabError("SELECTOR_CHANGED")
                href = candidates[0]["href"]
                expected_billing = parse_qs(urlsplit(href).query).get("billingcycle", [None])[0]
            else:
                if expected_billing and evidence["billing"] != expected_billing:
                    raise AutoGrabError("BILLING_MISMATCH")
                evidence["reused_configuration"] = True
                return evidence
        # Re-read the exact observed anchor immediately before the one dispatch.
        target = page.locator('a[href*="cart.php?a=add"]').filter(visible=True)
        fresh = await target.evaluate_all("nodes=>nodes.map(e=>e.href)")
        if fresh.count(href) != 1:
            raise AutoGrabError("UI_CHANGED")
        write_marker(marker_path, {"status": "DISPATCHED", "product_id": product.product_id})
        self.browser.policy.arm_config_get(href, product.product_id)
        # GET follows the observed link through the same browser/session.
        await self.browser.navigate(page, href)
        evidence = await self.browser.configuration_evidence(product)
        if expected_billing and evidence["billing"] != expected_billing:
            raise AutoGrabError("BILLING_MISMATCH")
        write_marker(marker_path, {"status": "VERIFIED", "product_id": product.product_id,
                                   "configuration_url": evidence["configuration_url"]})
        evidence["reused_configuration"] = False
        evidence["prior_verified_draft_expired_empty_cart_confirmed"] = recovered_empty
        return evidence

    async def dry_run_checkout(self, product, cart):
        # No click or form submission exists in this method.
        evidence = await self.browser.configuration_evidence(product)
        if any(evidence.get(key) != cart.get(key) for key in ("configuration_url", "billing", "selected_price", "product_name")):
            raise AutoGrabError("UI_CHANGED")
        return {**cart, "status": "DRY_RUN_BOUNDARY_REACHED", "boundary": "PRODUCT_CONFIGURATION_BEFORE_FORM_POST",
                "next_action": "SUBMIT_CART_CONFIGURATION_UNVERIFIED", "cart_url": "https://bandwagonhost.com/cart.php?a=view",
                "order_creation_step": "UNKNOWN_NOT_EXECUTED", "order_created": False,
                "cart_reserved": False, "payment_url": None}
