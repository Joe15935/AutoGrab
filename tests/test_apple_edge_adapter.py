"""Offline partial Apple handoff checks; never the real Edge profile or merchant."""
from pathlib import Path
import unittest

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-16/myev3ch/a"
EXPECTED = dict(product_id="cn:MYEV3CH/A", name="iPhone 16 / 128 GB / 黑色", url=URL,
                period="one_time", cents=599900, currency="CNY")


class AppleEdgeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(service_workers="block")
        self.routes, self.requests = {}, []

        async def offline(route):
            self.requests.append((route.request.method, route.request.url))
            if route.request.method == "GET" and route.request.url in self.routes:
                await route.fulfill(status=200, content_type="text/html", body=self.routes[route.request.url])
            else:
                await route.abort()

        await self.context.route("**/*", offline)
        self.page = await self.context.new_page()
        await self.page.add_init_script(path=ROOT / "edge-extension/providers/apple/adapter.js")

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def load(self, name, url=URL):
        self.routes[url] = (ROOT / "tests/fixtures" / name).read_text()
        await self.page.goto(url)
        self.requests.clear()

    async def call(self, method, expected=None):
        return await self.page.evaluate("([method, expected]) => AutoGrabApple[method](expected)", [method, expected or EXPECTED])

    async def test_disabled_control_stays_disabled_and_url_does_not_prove_selected_sku(self):
        await self.load("edge-apple-product-disabled.public.html")
        blocked = await self.call("detectPage")
        self.assertEqual((blocked["ok"], blocked["code"]), (False, "APPLE_BAG_UNAVAILABLE"))
        for method in ("selectBillingPeriod", "configureProduct", "addToCart", "openCheckout"):
            self.assertEqual((await self.call(method))["action"], "NONE")
        self.assertTrue(await self.page.locator("button").is_disabled())
        self.assertTrue(await self.page.evaluate("AutoGrabApple.readonly"))
        # Removing a control in this synthetic document must not create identity proof.
        await self.page.evaluate("document.querySelector('button').remove()")
        self.assertEqual((await self.call("detectPage"))["code"], "APPLE_SKU_PAGE_OBSERVED")
        self.assertEqual((await self.call("verifyProduct"))["code"], "APPLE_PRODUCT_IDENTITY_UNVERIFIED")
        self.assertFalse((await self.call("detectPage", {**EXPECTED,"product_id":"us:MYEV3CH/A"}))["ok"])
        await self.load("edge-apple-product-disabled.public.html", URL.replace("myev3ch", "myey3ch"))
        self.assertEqual((await self.call("detectPage"))["code"], "APPLE_SKU_MISMATCH")
        self.assertEqual(self.requests, [])

    async def test_only_observed_same_storefront_fulfillment_541_reports_bag_blocked(self):
        await self.load("edge-apple-product-disabled.public.html")
        for name, status, expected in (
            ("https://other.invalid/shop/fulfillment-messages", 541, "APPLE_BAG_UNAVAILABLE"),
            ("https://www.apple.com.cn/shop/retail/pickup-message", 541, "APPLE_BAG_UNAVAILABLE"),
            ("https://www.apple.com.cn/shop/fulfillment-messages", 200, "APPLE_BAG_UNAVAILABLE"),
            ("https://www.apple.com.cn/shop/fulfillment-messages?parts.0=MYEV3CH/A", 541, "APPLE_BAG_BLOCKED"),
        ):
            # Mimic native ResourceTiming fields only; no endpoint is requested.
            await self.page.evaluate("entry => performance.getEntriesByType = () => [entry]", {"name":name,"responseStatus":status})
            for method in ("detectPage", "verifyProduct", "verifyCart", "verifyCheckout", "addToCart"):
                result = await self.call(method)
                if expected == "APPLE_BAG_BLOCKED":
                    self.assertEqual((result["ok"], result["code"]), (False, expected))
                    self.assertNotIn("parts.0", str(result))
                elif method == "detectPage":
                    self.assertEqual(result["code"], expected)
            self.assertEqual(self.requests, [])
        self.assertTrue(await self.page.locator("button").is_disabled())

    async def test_generic_bag_text_is_not_verified_and_challenge_login_still_pause(self):
        await self.load("edge-apple-bag.synthetic.html", "https://www.apple.com.cn/shop/bag")
        page = await self.call("detectPage")
        self.assertEqual((page["stage"], page["code"]), ("CART", "APPLE_BAG_PAGE_OBSERVED"))
        self.assertEqual((await self.call("verifyCart"))["ok"], False)
        self.assertEqual((await self.call("verifyCheckout"))["ok"], False)
        self.assertEqual((await self.call("addToCart"))["action"], "NONE")
        await self.page.evaluate("document.title = 'Just a moment'")
        self.assertEqual((await self.call("detectPage"))["code"], "HUMAN_CHALLENGE_REQUIRED")
        await self.page.evaluate("document.title = 'Sign in'; document.body.insertAdjacentHTML('beforeend', '<input type=password>')")
        self.assertEqual((await self.call("detectPage"))["code"], "LOGIN_REQUIRED")
        self.assertEqual(self.requests, [])
