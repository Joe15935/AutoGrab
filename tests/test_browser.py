"""Offline Chromium integration: every permitted request is locally fulfilled.

These synthetic pages test the implementation, not BandwagonHost availability.
No request in this suite uses route.continue_ or a real merchant connection.
"""

from dataclasses import replace
import html
import json
from pathlib import Path
import tempfile
import unittest

from playwright.async_api import async_playwright

from autograb.browser.manager import BrowserManager, write_marker
from autograb.core.config import Config
from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from autograb.providers.bandwagon import BandwagonHostProvider


CATALOG = "https://bandwagonhost.com/order/basic/Vancouver/CABC_1"
CONFIGURATION = "https://bandwagonhost.com/cart.php?a=confproduct&i=0"
VIEW = "https://bandwagonhost.com/cart.php?a=view"
ADD = "https://bandwagonhost.com/cart.php?a=add&pid=44&billingcycle=annually&configoption%5B10%5D=39"
PRODUCT = Product(
    product_id="44", name="20G KVM - PROMO", availability="AVAILABLE",
    prices=[{"cents": 4999, "currency": "USD", "period": "Annually", "available": True}],
    product_url=CATALOG, order_url=ADD, eligible=True,
)
CONFIGURATION_FIXTURE = (
    Path(__file__).parent / "fixtures/configuration.public.sanitized.html"
).read_text(encoding="utf-8")


def configuration(name=PRODUCT.name, price="$49.99 Annually", script=""):
    return (CONFIGURATION_FIXTURE
            .replace("20G KVM - PROMO", html.escape(name))
            .replace("$49.99 Annually", html.escape(price))
            .replace("<!-- TEST_SCRIPT -->", script))


class _OfflineRoute:
    """Runs the production policy; permitted requests use fixture fulfillment."""

    def __init__(self, route, harness):
        self.request = route.request
        self.route, self.harness = route, harness

    async def continue_(self):
        self.harness.admitted.append((self.request.method, self.request.url))
        response = self.harness.responses.get(self.request.url)
        if response is None:
            await self.route.abort("failed")
            return
        status, body = response
        await self.route.fulfill(status=status, content_type="text/html", body=body)

    async def abort(self, reason):
        await self.route.abort(reason)


class OfflineBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(root=self.root, timeout_ms=1500)
        self.config.prepare()
        self.browser = BrowserManager(self.config)
        self.browser.playwright = await async_playwright().start()
        self.browser.context = await self.browser.playwright.chromium.launch_persistent_context(
            str(self.root / "profiles/bandwagon"), headless=True,
            service_workers="block", viewport={"width": 1000, "height": 700},
        )
        self.responses = {
            CATALOG: (200, f'<h1>Public fixture catalog</h1><a href="{html.escape(ADD, quote=True)}">Order</a>'),
            CONFIGURATION: (200, configuration()),
            ADD: (200, configuration(script="<script>history.replaceState({}, '', '/cart.php?a=confproduct&i=0')</script>")),
            VIEW: (200, "<h1>Order Summary</h1><p>Existing cart product</p>"),
        }
        self.admitted = []

        async def route(request_route):
            await self.browser.policy.route(_OfflineRoute(request_route, self))

        await self.browser.context.route("**/*", route)
        self.browser.context.set_default_timeout(1500)
        self.browser.page = await self.browser.context.new_page()
        self.browser.catalog_page = await self.browser.context.new_page()
        self.provider = BandwagonHostProvider(self.browser, None)

    async def asyncTearDown(self):
        await self.browser.close()
        self.temporary.cleanup()

    async def test_configuration_evidence_and_boundary_have_no_post(self):
        await self.browser.navigate(self.browser.page, CONFIGURATION)
        evidence = await self.browser.configuration_evidence(PRODUCT)
        self.assertTrue(evidence["configuration_verified"])
        self.assertEqual(evidence["product_id"], "44")
        self.assertEqual(evidence["billing"], "annually")
        self.assertIn("49.99", evidence["selected_price"])
        boundary = await self.provider.dry_run_checkout(PRODUCT, evidence)
        self.assertEqual(boundary["status"], "DRY_RUN_BOUNDARY_REACHED")
        self.assertFalse(boundary["order_created"])
        self.assertFalse(boundary["cart_reserved"])
        self.assertIsNone(boundary["payment_url"])
        self.assertTrue(all(method == "GET" for method, _ in self.admitted))

    async def test_product_mismatch_does_not_become_cart_ready(self):
        self.responses[CONFIGURATION] = (200, configuration(name="Entirely different plan"))
        await self.browser.navigate(self.browser.page, CONFIGURATION)
        with self.assertRaises(AutoGrabError) as error:
            await self.browser.configuration_evidence(PRODUCT)
        self.assertEqual(error.exception.code, "PRODUCT_MISMATCH")

    async def test_missing_real_price_is_not_configuration_evidence(self):
        self.responses[CONFIGURATION] = (200, configuration(price="Annually"))
        await self.browser.navigate(self.browser.page, CONFIGURATION)
        with self.assertRaises(AutoGrabError) as error:
            await self.browser.configuration_evidence(PRODUCT)
        self.assertEqual(error.exception.code, "SELECTOR_CHANGED")

    async def test_disabled_add_button_is_not_purchasable_configuration(self):
        self.responses[CONFIGURATION] = (200, configuration().replace('type="submit"', 'type="submit" disabled'))
        await self.browser.navigate(self.browser.page, CONFIGURATION)
        with self.assertRaises(AutoGrabError) as error:
            await self.browser.configuration_evidence(PRODUCT)
        self.assertEqual(error.exception.code, "SELECTOR_CHANGED")

    async def test_billing_mismatch_leaves_dispatch_uncertain_without_retry(self):
        await self.browser.navigate(self.browser.page, CATALOG)
        self.responses[ADD] = (200, configuration(
            price="$49.99 Monthly", script="<script>history.replaceState({}, '', '/cart.php?a=confproduct&i=0')</script>"
        ).replace('value="annually"', 'value="monthly"'))
        with self.assertRaises(AutoGrabError) as error:
            await self.provider.prepare_cart(PRODUCT)
        self.assertEqual(error.exception.code, "BILLING_MISMATCH")
        marker_path = self.root / "profiles/bandwagon/.cart-action.json"
        self.assertEqual(json.loads(marker_path.read_text())["status"], "DISPATCHED")
        with self.assertRaises(AutoGrabError) as error:
            await self.provider.prepare_cart(PRODUCT)
        self.assertEqual(error.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.assertEqual(self.admitted.count(("GET", ADD)), 1)

    async def test_configuration_text_on_catalog_url_is_not_cart(self):
        self.responses[CATALOG] = (200, configuration())
        await self.browser.navigate(self.browser.page, CATALOG)
        with self.assertRaises(AutoGrabError) as error:
            await self.browser.configuration_evidence(PRODUCT)
        self.assertEqual(error.exception.code, "UNEXPECTED_CART_PAGE")

    async def test_403_is_distinct_and_not_cart_success(self):
        self.responses[CATALOG] = (403, "<h1>Access denied</h1>")
        with self.assertRaises(AutoGrabError) as error:
            await self.browser.navigate(self.browser.catalog_page, CATALOG)
        self.assertEqual(error.exception.code, "HTTP_403")

    async def test_captcha_keeps_original_page_open(self):
        self.responses[CATALOG] = (403, "<h1>Verify you are human</h1><p>CAPTCHA required</p>")
        with self.assertRaises(AutoGrabError) as error:
            await self.browser.navigate(self.browser.catalog_page, CATALOG)
        self.assertEqual(error.exception.code, "CAPTCHA_REQUIRED")
        self.assertFalse(self.browser.catalog_page.is_closed())
        self.assertIn(self.browser.catalog_page, self.browser.context.pages)
        self.assertEqual(self.browser.catalog_page.url, CATALOG)

    async def test_login_required_keeps_page_and_does_not_submit(self):
        self.responses[CONFIGURATION] = (200, '<h1>Login</h1><form method="post" action="/dologin.php"><input type="password"><button>Login</button></form><script>history.replaceState({}, "", "/login.php")</script>')
        with self.assertRaises(AutoGrabError) as error:
            await self.browser.navigate(self.browser.page, CONFIGURATION)
        self.assertEqual(error.exception.code, "LOGIN_REQUIRED")
        self.assertFalse(self.browser.page.is_closed())
        self.assertFalse(any(method == "POST" for method, _ in self.admitted))

    async def test_explicit_manual_login_can_open_without_automatic_submission(self):
        login = "https://bandwagonhost.com/login.php"
        self.responses[login] = (200, '<h1>Login</h1><form method="post" action="/dologin.php"><input type="password"><button>Login</button></form>')
        self.browser.policy.login_mode = True
        await self.browser.navigate(self.browser.page, login)
        self.assertFalse(self.browser.page.is_closed())
        self.assertFalse(any(method == "POST" for method, _ in self.admitted))

    async def test_one_time_observed_get_cannot_be_replayed(self):
        self.browser.policy.arm_config_get(ADD, "44")
        await self.browser.navigate(self.browser.page, ADD)
        self.assertIsNone(self.browser.policy.ticket)
        with self.assertRaises(AutoGrabError):
            await self.browser.navigate(self.browser.page, ADD)
        self.assertEqual(self.admitted.count(("GET", ADD)), 1)
        self.assertTrue(any(item["path"] == "/cart.php" for item in self.browser.policy.blocked))

    async def test_final_order_post_is_blocked_by_actual_browser_routing(self):
        await self.browser.navigate(self.browser.page, CONFIGURATION)
        result = await self.browser.page.evaluate("""async () => {
            try { await fetch('/cart.php?a=checkout', {method:'POST', body:'fixture=not-an-order'}); return 'unexpected'; }
            catch (_) { return 'blocked'; }
        }""")
        self.assertEqual(result, "blocked")
        self.assertFalse(any(method == "POST" for method, _ in self.admitted))
        self.assertIn({"method": "POST", "path": "/cart.php", "resource_type": "fetch"}, self.browser.policy.blocked)

    async def test_dispatched_failure_is_persisted_and_never_replayed(self):
        await self.browser.navigate(self.browser.page, CATALOG)
        self.responses[ADD] = None
        self.responses[VIEW] = (200, "<h1>Order Summary</h1><p>Your Shopping Cart is Empty</p>")
        with self.assertRaises(AutoGrabError):
            await self.provider.prepare_cart(PRODUCT)
        marker_path = self.root / "profiles/bandwagon/.cart-action.json"
        self.assertEqual(json.loads(marker_path.read_text())["status"], "DISPATCHED")
        restarted_provider = BandwagonHostProvider(self.browser, None)
        with self.assertRaises(AutoGrabError) as error:
            await restarted_provider.prepare_cart(PRODUCT)
        self.assertEqual(error.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.assertEqual(self.admitted.count(("GET", ADD)), 1)
        self.assertEqual(self.admitted.count(("GET", VIEW)), 0)

    async def test_verified_configuration_is_reused_without_second_add(self):
        await self.browser.navigate(self.browser.page, CATALOG)
        evidence = await self.provider.prepare_cart(PRODUCT)
        self.assertFalse(evidence["reused_configuration"])
        await self.browser.navigate(self.browser.page, CATALOG)
        evidence = await BandwagonHostProvider(self.browser, None).prepare_cart(PRODUCT)
        self.assertTrue(evidence["reused_configuration"])
        self.assertEqual(self.admitted.count(("GET", ADD)), 1)

    async def test_expired_saved_cart_fails_without_readding(self):
        marker_path = self.root / "profiles/bandwagon/.cart-action.json"
        write_marker(marker_path, {"status": "VERIFIED", "product_id": "44", "configuration_url": CONFIGURATION})
        self.responses[CONFIGURATION] = (200, "<h1>Your cart is empty</h1>")
        await self.browser.navigate(self.browser.page, CATALOG)
        with self.assertRaises(AutoGrabError) as error:
            await self.provider.prepare_cart(PRODUCT)
        self.assertIn(error.exception.code, {"SELECTOR_CHANGED", "NETWORK_ERROR", "CART_REVIEW_REQUIRED", "CART_EXPIRY_UNVERIFIED"})
        self.assertEqual(self.admitted.count(("GET", ADD)), 0)
        self.assertEqual(json.loads(marker_path.read_text())["status"], "VERIFIED")

    async def test_verified_expired_cart_reconfigures_only_after_visible_empty_proof(self):
        marker_path = self.root / "profiles/bandwagon/.cart-action.json"
        write_marker(marker_path, {"status": "VERIFIED", "product_id": "44", "configuration_url": CONFIGURATION})
        self.responses[CONFIGURATION] = (200, "<h1>Expired configuration</h1>")
        self.responses[VIEW] = (200, "<h1>Order Summary</h1><p>Your Shopping Cart is Empty</p>")
        await self.browser.navigate(self.browser.page, CATALOG)
        evidence = await self.provider.prepare_cart(PRODUCT)
        self.assertTrue(evidence["prior_verified_draft_expired_empty_cart_confirmed"])
        self.assertFalse(evidence["reused_configuration"])
        self.assertEqual(self.admitted.count(("GET", ADD)), 1)
        self.assertLess(self.admitted.index(("GET", VIEW)), self.admitted.index(("GET", ADD)))
        self.assertEqual(json.loads(marker_path.read_text())["status"], "VERIFIED")

    async def test_existing_other_product_cart_is_not_replaced(self):
        marker_path = self.root / "profiles/bandwagon/.cart-action.json"
        write_marker(marker_path, {"status": "VERIFIED", "product_id": "44", "configuration_url": CONFIGURATION})
        with self.assertRaises(AutoGrabError) as error:
            await self.provider.prepare_cart(replace(PRODUCT, product_id="999"))
        self.assertEqual(error.exception.code, "CART_REVIEW_REQUIRED")
        self.assertEqual(self.admitted, [("GET", VIEW)])

    async def test_artifact_targets_failed_catalog_not_old_configuration(self):
        await self.browser.navigate(self.browser.page, CONFIGURATION)
        self.responses[CATALOG] = (403, "<h1>CAPTCHA required</h1>")
        with self.assertRaises(AutoGrabError):
            await self.browser.navigate(self.browser.catalog_page, CATALOG)
        result = await self.browser.artifact("CAPTCHA_REQUIRED")
        snapshot = self.root / result["directory"] / "page.sanitized.html"
        self.assertIn("CAPTCHA required", snapshot.read_text())
        self.assertNotIn(PRODUCT.name, snapshot.read_text())

    async def test_private_login_artifact_does_not_save_screenshot_or_values(self):
        self.responses[CONFIGURATION] = (200, '<h1>Login</h1><input type="password" value="fixture-private-value">')
        await self.browser.navigate(self.browser.page, CONFIGURATION)
        result = await self.browser.artifact("LOGIN_REQUIRED")
        directory = self.root / result["directory"]
        self.assertEqual(result["screenshot"], "SUPPRESSED_PRIVATE_PAGE")
        self.assertFalse((directory / "page.png").exists())
        self.assertNotIn("fixture-private-value", (directory / "page.sanitized.html").read_text())


if __name__ == "__main__":
    unittest.main()
