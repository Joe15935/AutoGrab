"""Offline DMIT DOM diagnostics; no authenticated browser or live merchant calls."""
from pathlib import Path
import unittest

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
CONFIG = "https://www.dmit.io/cart.php?a=confproduct&i=0"
CART = "https://www.dmit.io/cart.php?a=view"
CHECKOUT = "https://www.dmit.io/cart.php?a=checkout"


class DMITEdgeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(service_workers="block")
        self.requests = []
        html = (ROOT / "tests/fixtures/edge-dmit-configuration-pending.html").read_text()
        self.routes = {CONFIG: html}

        async def offline(route):
            self.requests.append((route.request.method, route.request.url))
            if route.request.url in self.routes and route.request.method == "GET":
                await route.fulfill(status=200, content_type="text/html", body=self.routes[route.request.url])
            else:
                await route.abort()

        await self.context.route("**/*", offline)
        self.page = await self.context.new_page()
        await self.page.add_init_script(path=ROOT / "edge-extension/providers/dmit/adapter.js")
        await self.page.goto(CONFIG)
        self.requests.clear()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def call(self, method):
        return await self.page.evaluate("method => AutoGrabDMIT[method]()", method)

    async def test_pending_configuration_is_uncertain_without_retry_or_cart_claim(self):
        page = await self.call("detectPage")
        self.assertFalse(page["ok"])
        self.assertEqual((page["stage"], page["code"]), ("CONFIGURATION", "CONFIGURATION_UNCERTAIN"))
        self.assertEqual((page["login"], page["challenge"]), ("UNKNOWN", "NONE"))
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div id=containerProductValidationErrors><ul id=containerProductValidationErrorsList><li>Invalid Linux hostname</li></ul></div>')")
        self.assertEqual(await self.call("detectPage"), page)
        self.assertTrue(await self.page.evaluate("AutoGrabDMIT.readonly"))
        for method in ("selectBillingPeriod", "configureProduct", "addToCart", "openCheckout"):
            result = await self.call(method)
            self.assertFalse(result["ok"])
            self.assertEqual((result["code"], result["action"]), ("ADAPTER_READ_ONLY", "NONE"))
        for method in ("verifyProduct", "verifyCart", "verifyCheckout"):
            self.assertFalse((await self.call(method))["ok"])
        self.assertEqual(self.requests, [])

    async def test_summary_spinner_or_hidden_button_spinner_is_not_pending_continue(self):
        await self.page.evaluate("document.querySelector('#btnCompleteProductConfig i').style.display = 'none'")
        hidden = await self.call("detectPage")
        self.assertTrue(hidden["ok"])
        self.assertEqual((hidden["stage"], hidden["code"]), ("CONFIGURATION", "CONFIGURATION_PAGE_OBSERVED"))
        await self.page.evaluate("document.querySelector('#btnCompleteProductConfig i').remove()")
        normal = await self.call("detectPage")
        self.assertEqual(normal, hidden)
        await self.page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div id=containerProductValidationErrors style=display:none><ul id=containerProductValidationErrorsList><li>Invalid Linux hostname</li></ul></div>')")
        self.assertEqual(await self.call("detectPage"), normal)
        await self.page.evaluate("document.querySelector('#containerProductValidationErrors').style.display = 'block'")
        invalid = await self.call("detectPage")
        self.assertFalse(invalid["ok"])
        self.assertEqual((invalid["stage"], invalid["code"]), ("CONFIGURATION", "CONFIGURATION_INVALID"))
        self.assertNotIn("hostname", str(invalid).lower())
        await self.page.evaluate("document.querySelector('#containerProductValidationErrorsList').textContent = ''")
        self.assertEqual(await self.call("detectPage"), normal)
        self.assertEqual(self.requests, [])

    async def test_challenge_still_overrides_configuration_diagnostic(self):
        await self.page.evaluate("document.title = 'Just a moment'")
        result = await self.call("detectPage")
        self.assertFalse(result["ok"])
        self.assertEqual((result["stage"], result["code"]), ("HUMAN_CHALLENGE", "HUMAN_CHALLENGE_REQUIRED"))
        self.assertEqual(self.requests, [])

    async def test_observed_cart_and_checkout_stages_do_not_claim_identity_or_dispatch(self):
        html = (ROOT / "tests/fixtures/edge-dmit-cart-checkout.html").read_text()
        for url, stage, verify in ((CART, "CART", "verifyCart"), (CHECKOUT, "CHECKOUT", "verifyCheckout")):
            self.routes[url] = html
            await self.page.goto(url)
            self.requests.clear()
            result = await self.call("detectPage")
            self.assertTrue(result["ok"])
            self.assertEqual((result["stage"], result["code"]), (stage, stage + "_PAGE_OBSERVED"))
            self.assertNotIn("cart_id", result)
            self.assertFalse((await self.call(verify))["ok"])
            self.assertEqual((await self.call("openCheckout"))["action"], "NONE")
            if stage == "CART":
                await self.page.evaluate("document.querySelector('#cart a').href = 'https://other.invalid/cart.php?a=checkout'")
            else:
                await self.page.evaluate("document.querySelector('#checkout button').hidden = true")
            missing = await self.call("detectPage")
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["stage"], "UNKNOWN")
            await self.page.evaluate("document.title = 'Just a moment'")
            self.assertEqual((await self.call("detectPage"))["code"], "HUMAN_CHALLENGE_REQUIRED")
            self.assertEqual(self.requests, [])
