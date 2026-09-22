"""Offline DOM checks; these never access real accounts or prove live checkout."""
from pathlib import Path
import unittest
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("vps", "AutoGrabVPS", "vps-public-card.html", dict(product_id="148", name="NRT Starter",
        url="https://v.ps/products/cloud-kvm-vps/", cents=695, currency="EUR", period="monthly")),
    ("vmiss", "AutoGrabVMISS", "vmiss-contract.html", dict(product_id="us-los-angeles-bgp/basic", name="Test Basic",
        url="https://app.vmiss.com/store/us-los-angeles-bgp", cents=500, currency="CAD", period="monthly")),
)


class PublicEdgeReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(service_workers="block")
        self.routes, self.requests = {}, []

        async def offline(route):
            self.requests.append((route.request.method, route.request.url))
            if route.request.url in self.routes:
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=self.routes[route.request.url])
            else:
                await route.abort()

        await self.context.route("**/*", offline)
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def load(self, case, html=None):
        provider, name, fixture, expected = case
        self.routes[expected["url"]] = html if html is not None else (ROOT / "tests/fixtures" / fixture).read_text()
        await self.page.goto(expected["url"])
        await self.page.add_script_tag(path=ROOT / "edge-extension/providers" / provider / "adapter.js")
        self.requests.clear()
        return name, expected

    async def call(self, name, method, expected):
        return await self.page.evaluate("([name, method, expected]) => globalThis[name][method](expected)", [name, method, expected])

    async def test_public_product_binding_and_all_mutations_refused(self):
        for case in CASES:
            with self.subTest(provider=case[0]):
                name, expected = await self.load(case)
                self.assertTrue((await self.call(name, "verifyProduct", expected))["ok"])
                self.assertFalse((await self.call(name, "verifyProduct", {**expected, "cents": 1}))["ok"])
                for method in ("selectBillingPeriod", "configureProduct", "addToCart", "openCheckout"):
                    result = await self.call(name, method, expected)
                    self.assertEqual((result["ok"], result["action"], result["code"]), (False, "NONE", "ADAPTER_READ_ONLY"))
                for method in ("verifyCart", "verifyCheckout", "detectOrder", "detectInvoice", "detectPaymentReady"):
                    self.assertFalse((await self.call(name, method, expected))["ok"])
                self.assertEqual(self.requests, [])

    async def test_challenge_login_and_unrecognized_pages_never_verify_product(self):
        for case in CASES:
            for html, expected_code in (("<title>Just a moment</title><p>Verify you are human</p>", "HUMAN_CHALLENGE_REQUIRED"),
                                        ('<h1>Login</h1><input type="password">', "LOGIN_REQUIRED"),
                                        ("<h1>Unexpected site redesign</h1>", None)):
                with self.subTest(provider=case[0], code=expected_code):
                    name, expected = await self.load(case, html)
                    result = await self.call(name, "verifyProduct", expected)
                    self.assertFalse(result["ok"])
                    if expected_code:
                        self.assertEqual(result["code"], expected_code)
                    self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
