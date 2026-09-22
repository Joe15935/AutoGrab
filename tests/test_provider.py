"""Provider behavior with real parsing/persistence and mocked network/browser I/O.

These tests never launch a browser or make a network request. They do not prove
current site reachability, stock, cart access, or a real login session.
"""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call

from autograb.browser.safety import SafetyPolicy
from autograb.browser.manager import BrowserManager
from autograb.core.errors import AutoGrabError
from autograb.providers.bandwagon import BandwagonHostProvider, CATALOG_PAGE
from autograb.providers.catalog import parse_catalog


FIXTURE = Path(__file__).parent / "fixtures" / "catalog.small.json"
ADD = "https://bandwagonhost.com/cart.php?a=add&pid=44&billingcycle=annually&configoption%5B10%5D=39"
CONFIGURATION = "https://bandwagonhost.com/cart.php?a=confproduct&i=0"


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "profiles/bandwagon").mkdir(parents=True)
        self.marker = self.root / "profiles/bandwagon/.cart-action.json"
        self.payload = json.loads(FIXTURE.read_text())
        self.products = parse_catalog(self.payload)
        self.product = self.products[0]
        self.embedded = SimpleNamespace(all_text_contents=AsyncMock(return_value=[]))
        self.catalog_page = SimpleNamespace(
            url=CATALOG_PAGE,
            evaluate=AsyncMock(return_value={"status": 200, "text": json.dumps(self.payload)}),
            locator=Mock(return_value=self.embedded),
        )
        self.links = SimpleNamespace(evaluate_all=AsyncMock(return_value=[]))
        self.links.filter = Mock(return_value=self.links)
        self.links.first = SimpleNamespace(wait_for=AsyncMock())
        self.radio = SimpleNamespace(count=AsyncMock(return_value=1), check=AsyncMock(), is_checked=AsyncMock(return_value=True))
        self.page = SimpleNamespace(url=self.product.product_url, locator=Mock(return_value=self.links), get_by_role=Mock(return_value=self.radio))
        policy = SafetyPolicy()
        policy.arm_config_get = Mock(wraps=policy.arm_config_get)
        self.evidence = {
            "product_id": self.product.product_id,
            "product_name": self.product.name,
            "billing": "annually",
            "selected_price": "$49.99 USD Annually",
            "configuration_url": CONFIGURATION,
            "configuration_verified": True,
        }
        self.browser = SimpleNamespace(
            config=SimpleNamespace(root=self.root, timeout_ms=300),
            catalog_page=self.catalog_page,
            page=self.page,
            policy=policy,
            start=AsyncMock(),
            navigate=AsyncMock(),
            guard_page=AsyncMock(),
            configuration_evidence=AsyncMock(side_effect=lambda product: dict(self.evidence)),
            confirm_empty_cart=AsyncMock(return_value=False),
        )
        self.log = Mock()
        self.provider = BandwagonHostProvider(self.browser, self.log)
        # Every test instance replaces this sync I/O boundary before any real
        # provider method runs, including when called through asyncio.to_thread.
        self.provider._http = Mock(return_value=(403, None))

    def write_marker(self, status, product_id=None, **extra):
        self.marker.write_text(json.dumps({
            "status": status,
            "product_id": product_id or self.product.product_id,
            **extra,
        }))

    async def test_http_403_is_attempted_once_and_browser_fetch_continues(self):
        first = await self.provider.discover_products()
        second = await self.provider.discover_products()
        self.assertEqual([product.product_id for product in first], ["44", "999", "1000"])
        self.assertEqual(first, second)
        self.provider._http.assert_called_once()
        self.assertEqual(self.provider.routes["A_LIGHTWEIGHT_HTTP"], {"status": "UNSUPPORTED", "http_status": 403})
        self.assertEqual(self.provider.routes["B_BROWSER_FETCH"]["status"], "SUPPORTED")
        self.assertEqual(self.provider.source, "B_BROWSER_FETCH")
        self.assertEqual(self.catalog_page.evaluate.await_count, 2)

    async def test_browser_json_is_preferred_over_dom_json(self):
        self.embedded.all_text_contents.return_value = [json.dumps(self.payload)]
        products = await self.provider.discover_products()
        self.assertEqual(products, self.products)
        self.assertEqual(self.provider.source, "B_BROWSER_FETCH")

    async def test_dom_json_fallback_skips_unrelated_and_malformed_nodes(self):
        self.catalog_page.evaluate.return_value = {"status": 200, "text": "No JSON here"}
        self.embedded.all_text_contents.return_value = ["{invalid", '{"unrelated": true}', json.dumps(self.payload)]
        products = await self.provider.discover_products()
        self.assertEqual(products, self.products)
        self.assertEqual(self.provider.source, "C_DOM_JSON")
        self.assertEqual(self.provider.routes["B_BROWSER_FETCH"]["status"], "UNSUPPORTED")
        self.assertEqual(self.provider.routes["C_DOM_JSON"]["status"], "SUPPORTED")
        self.assertEqual(self.provider.routes["C_DOM_JSON"]["json_nodes"], 3)
        self.browser.navigate.assert_awaited_once_with(self.catalog_page, CATALOG_PAGE)

    async def test_supported_http_is_fallback_when_browser_sources_are_unavailable(self):
        self.provider._http.return_value = (200, self.payload)
        self.catalog_page.evaluate.return_value = {"status": 200, "text": "No JSON here"}
        products = await self.provider.discover_products()
        self.assertEqual(products, self.products)
        self.assertEqual(self.provider.source, "A_LIGHTWEIGHT_HTTP")

    async def test_no_valid_json_rejects_snapshot(self):
        self.catalog_page.evaluate.return_value = {"status": 200, "text": "<html>No JSON</html>"}
        self.embedded.all_text_contents.return_value = ['{"products": []}', '{"other": true}']
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.discover_products()
        self.assertEqual(raised.exception.code, "DATA_SOURCE_UNAVAILABLE")
        self.assertEqual(self.provider.last_products, [])
        self.assertIsNone(self.provider.source)
        self.browser.guard_page.assert_awaited_once_with(self.catalog_page)

    async def test_browser_403_stops_without_using_a_stale_dom_snapshot(self):
        self.embedded.all_text_contents.return_value = [json.dumps(self.payload)]
        for body, code in [("Forbidden", "HTTP_403"), ("Verify you are human", "CAPTCHA_REQUIRED"), ("Cloudflare denied", "CLOUDFLARE")]:
            self.catalog_page.evaluate.return_value = {"status": 403, "text": body}
            with self.subTest(code=code), self.assertRaises(AutoGrabError) as raised:
                await self.provider.discover_products()
            self.assertEqual(raised.exception.code, code)
        self.embedded.all_text_contents.assert_not_awaited()
        self.browser.navigate.assert_not_awaited()
        self.assertEqual(self.provider.last_products, [])

    async def test_dispatched_marker_stops_before_any_navigation_or_rearm(self):
        self.write_marker("DISPATCHED")
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.prepare_cart(self.product)
        self.assertEqual(raised.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.browser.navigate.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()
        self.page.locator.assert_not_called()
        self.browser.configuration_evidence.assert_not_awaited()
        self.browser.confirm_empty_cart.assert_not_awaited()

    async def test_existing_malformed_or_empty_marker_stops_without_browser_actions(self):
        for contents in ["{}", "[]", "null", "{invalid", '{"status": "VERIFIED"}']:
            self.marker.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(AutoGrabError) as raised:
                await self.provider.prepare_cart(self.product)
            self.assertEqual(raised.exception.code, "ACTION_OUTCOME_UNKNOWN")
            self.assertEqual(self.marker.read_text(), contents)
        self.browser.navigate.assert_not_awaited()
        self.browser.confirm_empty_cart.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()
        self.page.locator.assert_not_called()

    async def test_verified_marker_for_another_product_requires_review(self):
        self.write_marker("VERIFIED", product_id="45", configuration_url=CONFIGURATION)
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.prepare_cart(self.product)
        self.assertEqual(raised.exception.code, "CART_REVIEW_REQUIRED")
        self.browser.navigate.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()
        self.page.locator.assert_not_called()
        self.browser.confirm_empty_cart.assert_awaited_once()

    async def test_verified_same_product_rereads_configuration_without_add(self):
        self.write_marker("VERIFIED", configuration_url=CONFIGURATION)
        self.links.evaluate_all.return_value = [{"href": ADD, "text": "Order", "visible": True}]
        original_marker = self.marker.read_text()
        result = await self.provider.prepare_cart(self.product)
        self.assertTrue(result["reused_configuration"])
        self.browser.navigate.assert_awaited_once_with(self.page, CONFIGURATION)
        self.browser.configuration_evidence.assert_awaited_once_with(self.product)
        self.browser.policy.arm_config_get.assert_not_called()
        self.links.evaluate_all.assert_awaited_once()
        self.links.filter.assert_not_called()
        self.assertEqual(self.marker.read_text(), original_marker)

    async def test_expired_verified_configuration_requires_fresh_empty_cart_proof(self):
        self.write_marker("VERIFIED", configuration_url=CONFIGURATION)
        self.links.evaluate_all.side_effect = [
            [{"href": ADD, "text": "Order", "visible": True}],
            [{"href": ADD, "text": "Order", "visible": True}], [ADD],
        ]
        self.browser.configuration_evidence.side_effect = [AutoGrabError("SELECTOR_CHANGED"), dict(self.evidence)]
        self.browser.confirm_empty_cart.return_value = True

        async def dispatch(page, url):
            if url == ADD:
                self.assertEqual(json.loads(self.marker.read_text())["status"], "DISPATCHED")
                self.assertTrue(self.browser.policy.permits(url, "GET", "document"))

        self.browser.navigate.side_effect = dispatch
        result = await self.provider.prepare_cart(self.product)
        self.assertTrue(result["prior_verified_draft_expired_empty_cart_confirmed"])
        self.assertFalse(result["reused_configuration"])
        self.browser.confirm_empty_cart.assert_awaited_once()
        self.browser.policy.arm_config_get.assert_called_once_with(ADD, "44")
        self.assertEqual(self.browser.navigate.await_args_list, [
            call(self.page, CONFIGURATION), call(self.page, self.product.product_url), call(self.page, ADD),
        ])
        self.assertEqual(json.loads(self.marker.read_text())["status"], "VERIFIED")

    async def test_expired_configuration_with_nonempty_or_unconfirmed_cart_stops(self):
        self.write_marker("VERIFIED", configuration_url=CONFIGURATION)
        original_marker = self.marker.read_text()
        self.links.evaluate_all.return_value = [{"href": ADD, "text": "Order", "visible": True}]
        self.browser.configuration_evidence.side_effect = AutoGrabError("SELECTOR_CHANGED")
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.prepare_cart(self.product)
        self.assertEqual(raised.exception.code, "CART_REVIEW_REQUIRED")
        self.browser.navigate.assert_awaited_once_with(self.page, CONFIGURATION)
        self.browser.confirm_empty_cart.assert_awaited_once()
        self.browser.policy.arm_config_get.assert_not_called()
        self.assertEqual(self.marker.read_text(), original_marker)

    async def test_uncertain_verified_configuration_does_not_attempt_empty_cart_recovery(self):
        self.write_marker("VERIFIED", configuration_url=CONFIGURATION)
        original_marker = self.marker.read_text()
        self.links.evaluate_all.return_value = [{"href": ADD, "text": "Order", "visible": True}]
        for code in ["TIMEOUT", "CAPTCHA_REQUIRED", "PRODUCT_MISMATCH"]:
            self.browser.configuration_evidence.side_effect = AutoGrabError(code)
            with self.subTest(code=code), self.assertRaises(AutoGrabError) as raised:
                await self.provider.prepare_cart(self.product)
            self.assertEqual(raised.exception.code, code)
        self.browser.confirm_empty_cart.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()
        self.assertEqual(self.marker.read_text(), original_marker)

    async def test_other_verified_product_can_change_only_after_empty_cart_confirmation(self):
        self.write_marker("VERIFIED", product_id="45", configuration_url=CONFIGURATION)
        self.browser.confirm_empty_cart.return_value = True
        self.links.evaluate_all.side_effect = [
            [{"href": ADD, "text": "Order", "visible": True}], [ADD],
        ]

        async def dispatch(page, url):
            if url == ADD:
                self.assertEqual(json.loads(self.marker.read_text())["status"], "DISPATCHED")
                self.assertTrue(self.browser.policy.permits(url, "GET", "document"))

        self.browser.navigate.side_effect = dispatch
        result = await self.provider.prepare_cart(self.product)
        self.assertTrue(result["prior_verified_draft_expired_empty_cart_confirmed"])
        self.browser.confirm_empty_cart.assert_awaited_once()
        self.browser.policy.arm_config_get.assert_called_once_with(ADD, "44")
        self.assertEqual(self.browser.navigate.await_args_list, [
            call(self.page, self.product.product_url), call(self.page, ADD),
        ])
        self.assertEqual(json.loads(self.marker.read_text())["product_id"], "44")

    async def test_unsafe_or_ambiguous_observed_links_stop_before_dispatch(self):
        for links in [
            [],
            [{"href": ADD.replace("bandwagonhost.com", "example.com"), "text": "Order", "visible": True}],
            [{"href": ADD.replace("pid=44", "pid=45"), "text": "Order", "visible": True}],
            [{"href": ADD, "text": "Order", "visible": False}],
            [{"href": ADD, "text": "Order", "visible": True}] * 2,
        ]:
            self.links.evaluate_all.return_value = links
            with self.subTest(links=links), self.assertRaises(AutoGrabError) as raised:
                await self.provider.prepare_cart(self.product)
            self.assertEqual(raised.exception.code, "SELECTOR_CHANGED")
            self.assertFalse(self.marker.exists())
        self.browser.navigate.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()

    async def test_changed_observed_link_stops_before_persisting_dispatch(self):
        self.links.evaluate_all.side_effect = [
            [{"href": ADD, "text": "Order", "visible": True}],
            [ADD.replace("pid=44", "pid=45")],
        ]
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.prepare_cart(self.product)
        self.assertEqual(raised.exception.code, "UI_CHANGED")
        self.assertFalse(self.marker.exists())
        self.browser.navigate.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()

    async def test_uncertain_readback_persists_dispatch_and_prevents_replay(self):
        self.links.evaluate_all.side_effect = [
            [{"href": ADD, "text": "Order", "visible": True}], [ADD],
        ]

        async def dispatch(page, url):
            self.assertEqual(json.loads(self.marker.read_text())["status"], "DISPATCHED")
            self.assertTrue(self.browser.policy.permits(url, "GET", "document"))

        self.browser.navigate.side_effect = dispatch
        self.browser.configuration_evidence.side_effect = AutoGrabError("TIMEOUT")
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.prepare_cart(self.product)
        self.assertEqual(raised.exception.code, "TIMEOUT")
        self.assertEqual(json.loads(self.marker.read_text())["status"], "DISPATCHED")
        with self.assertRaises(AutoGrabError) as repeated:
            await self.provider.prepare_cart(self.product)
        self.assertEqual(repeated.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.browser.navigate.assert_awaited_once_with(self.page, ADD)
        self.browser.policy.arm_config_get.assert_called_once_with(ADD, self.product.product_id)
        self.assertIsNone(self.browser.policy.ticket)

    async def test_verified_dispatch_persists_configuration_for_later_reread(self):
        self.links.evaluate_all.side_effect = [
            [{"href": ADD, "text": "Order", "visible": True}], [ADD],
        ]

        async def dispatch(page, url):
            self.assertEqual(json.loads(self.marker.read_text())["status"], "DISPATCHED")
            self.assertTrue(self.browser.policy.permits(url, "GET", "document"))

        self.browser.navigate.side_effect = dispatch
        result = await self.provider.prepare_cart(self.product)
        self.assertFalse(result["reused_configuration"])
        self.assertEqual(json.loads(self.marker.read_text()), {
            "status": "VERIFIED", "product_id": "44", "configuration_url": CONFIGURATION,
        })
        self.browser.navigate.assert_awaited_once_with(self.page, ADD)

    async def test_dry_run_boundary_only_rereads_and_never_dispatches(self):
        result = await self.provider.dry_run_checkout(self.product, dict(self.evidence))
        self.assertEqual(result["status"], "DRY_RUN_BOUNDARY_REACHED")
        self.assertFalse(result["order_created"])
        self.assertFalse(result["cart_reserved"])
        self.assertIsNone(result["payment_url"])
        self.browser.navigate.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()
        self.browser.configuration_evidence.assert_awaited_once_with(self.product)

    async def test_billing_change_between_prepare_and_boundary_stops(self):
        cart = dict(self.evidence)
        self.evidence.update(billing="monthly", selected_price="$5.00 USD Monthly")
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.dry_run_checkout(self.product, cart)
        self.assertEqual(raised.exception.code, "UI_CHANGED")
        self.browser.navigate.assert_not_awaited()
        self.browser.policy.arm_config_get.assert_not_called()

    async def test_verified_configuration_with_wrong_billing_is_not_reused(self):
        self.write_marker("VERIFIED", configuration_url=CONFIGURATION)
        original_marker = self.marker.read_text()
        self.links.evaluate_all.return_value = [{"href": ADD, "text": "Order", "visible": True}]
        self.evidence["billing"] = "monthly"
        with self.assertRaises(AutoGrabError) as raised:
            await self.provider.prepare_cart(self.product)
        self.assertEqual(raised.exception.code, "BILLING_MISMATCH")
        self.assertEqual(self.marker.read_text(), original_marker)
        self.browser.policy.arm_config_get.assert_not_called()

    async def test_check_product_stops_on_unknown_sold_out_or_missing_stock(self):
        self.provider.discover_products = AsyncMock(return_value=self.products)
        for product_id, code in [("999", "OUT_OF_STOCK"), ("1000", "STOCK_UNKNOWN"), ("9999", "PRODUCT_MISSING")]:
            with self.subTest(product_id=product_id), self.assertRaises(AutoGrabError) as raised:
                await self.provider.check_product(product_id)
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(await self.provider.check_product("44"), self.product)


class EmptyCartProofTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.empty = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
        self.summary = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
        self.page = SimpleNamespace(
            url="https://bandwagonhost.com/cart.php?a=view",
            get_by_text=Mock(return_value=self.empty),
            get_by_role=Mock(return_value=self.summary),
        )
        self.browser = SimpleNamespace(page=self.page, navigate=AsyncMock())

    async def test_empty_cart_proof_requires_fresh_navigation_and_exact_visible_evidence(self):
        self.assertTrue(await BrowserManager.confirm_empty_cart(self.browser))
        self.browser.navigate.assert_awaited_once_with(self.page, "https://bandwagonhost.com/cart.php?a=view")
        self.page.get_by_text.assert_called_once_with("Your Shopping Cart is Empty", exact=True)
        self.page.get_by_role.assert_called_once_with("heading", name="Order Summary", exact=True)

    async def test_empty_cart_proof_rejects_redirect_missing_ambiguous_or_hidden_evidence(self):
        for url, empty_count, empty_visible, summary_count, summary_visible in [
            ("https://bandwagonhost.com/clientarea.php", 1, True, 1, True),
            ("https://bandwagonhost.com/cart.php?a=view", 0, True, 1, True),
            ("https://bandwagonhost.com/cart.php?a=view", 2, True, 1, True),
            ("https://bandwagonhost.com/cart.php?a=view", 1, False, 1, True),
            ("https://bandwagonhost.com/cart.php?a=view", 1, True, 0, True),
            ("https://bandwagonhost.com/cart.php?a=view", 1, True, 2, True),
            ("https://bandwagonhost.com/cart.php?a=view", 1, True, 1, False),
        ]:
            self.page.url = url
            self.empty.count.return_value = empty_count
            self.empty.is_visible.return_value = empty_visible
            self.summary.count.return_value = summary_count
            self.summary.is_visible.return_value = summary_visible
            with self.subTest(url=url, empty_count=empty_count, empty_visible=empty_visible,
                              summary_count=summary_count, summary_visible=summary_visible):
                self.assertFalse(await BrowserManager.confirm_empty_cart(self.browser))


if __name__ == "__main__":
    unittest.main()
