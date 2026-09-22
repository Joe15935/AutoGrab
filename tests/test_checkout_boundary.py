"""Offline Phase 2 policy and provider boundary review; no account/network access.

Fixtures exercise only known public cart navigation. They never establish that
an authenticated real checkout creates an unpaid order without charging it.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from autograb.browser.checkout_policy import CheckoutPolicy
from autograb.browser.safety import SafetyPolicy
from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from autograb.providers.bandwagon import BandwagonHostProvider
from autograb.providers.checkout import BandwagonPaymentProvider


CONFIGURATION = "https://bandwagonhost.com/cart.php?a=confproduct&i=0"
CHECKOUT = "https://bandwagonhost.com/cart.php?a=checkout"
ACCOUNT = "https://bandwagonhost.com/clientarea.php"
INVOICE = "https://bandwagonhost.com/viewinvoice.php?id=456"
PRODUCT = Product(
    product_id="44", name="20G KVM - PROMO", availability="AVAILABLE",
    prices=[{"cents": 4999, "currency": "USD", "period": "Annually"}],
    product_url="https://bandwagonhost.com/order/basic", eligible=True,
)


def observed_checkout():
    return {
        "loginRequired": True,
        "forms": [{"method": "post", "action": CHECKOUT}],
        "gatewayNames": ["paypal", "fixture-gateway"],
        "termsRequired": True,
        "submitLabels": ["Complete Order"],
    }


class CheckoutPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = CheckoutPolicy()

    def test_defaults_preserve_phase_one_read_boundary(self):
        for url in (CHECKOUT, ACCOUNT, INVOICE):
            with self.subTest(url=url):
                self.assertFalse(self.policy.permits(url, "GET", "document"))
        self.assertFalse(self.policy.permits(CONFIGURATION, "POST", "document"))
        self.assertTrue(self.policy.permits(CONFIGURATION, "GET", "document"))

    def test_single_exact_configuration_post_ticket_is_consumed_once(self):
        self.policy.allow_configuration_post(CONFIGURATION)
        self.assertTrue(self.policy.permits(CONFIGURATION, "POST", "document"))
        self.assertFalse(self.policy.permits(CONFIGURATION, "POST", "document"))

    def test_get_background_or_wrong_configuration_cannot_consume_post_ticket(self):
        self.policy.allow_configuration_post(CONFIGURATION)
        for method, kind, url in (
            ("POST", "xhr", CONFIGURATION), ("POST", "fetch", CONFIGURATION),
            ("POST", "document", CONFIGURATION.replace("i=0", "i=1")),
            ("POST", "document", CONFIGURATION + "&checkout=1"),
        ):
            with self.subTest(method=method, kind=kind, url=url):
                self.assertFalse(self.policy.permits(url, method, kind))
        self.assertTrue(self.policy.permits(CONFIGURATION, "GET", "document"))
        self.assertTrue(self.policy.permits(CONFIGURATION, "POST", "document"))

    def test_unspent_post_ticket_cannot_be_replaced(self):
        self.policy.allow_configuration_post(CONFIGURATION)
        with self.assertRaises(ValueError):
            self.policy.allow_configuration_post(CONFIGURATION.replace("i=0", "i=1"))
        self.assertTrue(self.policy.permits(CONFIGURATION, "POST", "document"))

    def test_configuration_ticket_rejects_unknown_or_ambiguous_endpoint(self):
        for url in (
            CHECKOUT, CONFIGURATION.replace("https:", "http:"),
            CONFIGURATION.replace("bandwagonhost.com", "user:fixture@bandwagonhost.com"),
            CONFIGURATION.replace("bandwagonhost.com", "bandwagonhost.com.other.test"),
            CONFIGURATION.replace("/cart.php", "/cart%2ephp"),
            CONFIGURATION.replace("i=0", "i=-1"), CONFIGURATION.replace("i=0", "i=０"),
            CONFIGURATION + "&i=0", CONFIGURATION + "&a=checkout", CONFIGURATION + "#fragment",
            CONFIGURATION + "&token=fixture", CONFIGURATION + "\n", "https://[invalid",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.policy.allow_configuration_post(url)

    def test_checkout_get_requires_explicit_read_flag(self):
        self.assertFalse(self.policy.permits(CHECKOUT, "GET", "document"))
        self.policy.checkout_reads = True
        self.assertTrue(self.policy.permits(CHECKOUT, "GET", "document"))
        self.assertFalse(self.policy.permits(CHECKOUT, "GET", "fetch"))
        self.assertFalse(self.policy.permits(CHECKOUT + "&submit=1", "GET", "document"))

    def test_account_read_flag_has_narrow_official_allowlist(self):
        self.policy.account_reads = True
        for url in (ACCOUNT, "https://bandwagonhost.com/login.php", ACCOUNT + "?action=invoices",
                    ACCOUNT + "?action=products", INVOICE):
            with self.subTest(url=url):
                self.assertTrue(self.policy.permits(url, "GET", "document"))
                self.assertFalse(self.policy.permits(url, "POST", "document"))
        for url in (ACCOUNT + "?action=cancel", INVOICE + "&pay=1", INVOICE + "&id=456",
                    INVOICE.replace("id=456", "id=0"), INVOICE.replace("id=456", "id=４５６")):
            with self.subTest(url=url):
                self.assertFalse(self.policy.permits(url, "GET", "document"))

    def test_final_checkout_and_payment_endpoints_never_enabled_by_flags(self):
        self.policy.account_reads = self.policy.checkout_reads = self.policy.login_mode = True
        # Unknown configuration flags do not grant network authority.
        self.policy.mode, self.policy.armed = "LIVE", True
        for url in (CHECKOUT, "https://bandwagonhost.com/cart.php?a=complete", INVOICE,
                    "https://bandwagonhost.com/creditcard.php", "https://bandwagonhost.com/paypal.php",
                    "https://bandwagonhost.com/modules/gateways/callback/paypal.php",
                    "https://www.paypal.com/checkoutnow?token=fixture"):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(url=url, method=method):
                    self.assertFalse(self.policy.permits(url, method, "document"))
        for url in ("https://bandwagonhost.com/creditcard.php", "https://bandwagonhost.com/paypal.php",
                    "https://www.paypal.com/checkoutnow?token=fixture"):
            self.assertFalse(self.policy.permits(url, "GET", "document"))

    def test_live_flags_do_not_turn_configuration_ticket_into_order_ticket(self):
        self.policy.allow_configuration_post(CONFIGURATION)
        self.policy.account_reads = self.policy.checkout_reads = True
        self.assertFalse(self.policy.permits(CHECKOUT, "POST", "document"))
        self.assertTrue(self.policy.permits(CONFIGURATION, "POST", "document"))
        self.assertFalse(self.policy.permits(CHECKOUT, "POST", "document"))

    def test_manual_login_exception_does_not_grant_order_authority(self):
        self.policy.login_mode = True
        self.assertTrue(self.policy.permits("https://bandwagonhost.com/dologin.php", "POST", "document"))
        self.assertFalse(self.policy.permits(CHECKOUT, "POST", "document"))


class CheckoutRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_final_submit_never_dispatches_or_logs_private_query(self):
        policy = CheckoutPolicy()
        policy.checkout_reads = policy.account_reads = True
        route = SimpleNamespace(
            request=SimpleNamespace(url=CHECKOUT + "&token=fixture-private-value", method="POST", resource_type="document"),
            continue_=AsyncMock(), abort=AsyncMock(),
        )
        await policy.route(route)
        route.continue_.assert_not_awaited()
        route.abort.assert_awaited_once_with("blockedbyclient")
        self.assertEqual(policy.blocked, [{"method": "POST", "path": "/cart.php", "resource_type": "document"}])
        self.assertNotIn("fixture-private-value", str(policy.blocked))


class CheckoutProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.password = SimpleNamespace(count=AsyncMock(return_value=0))
        self.logout = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
        self.body = SimpleNamespace(inner_text=AsyncMock(return_value="Fixture account portal"))
        self.page = SimpleNamespace(
            url=ACCOUNT, goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            title=AsyncMock(return_value="Fixture Client Area"), locator=Mock(side_effect=self.locator),
            evaluate=AsyncMock(return_value=observed_checkout()),
            get_by_role=Mock(return_value=SimpleNamespace(count=AsyncMock(return_value=1))),
        )
        self.browser = SimpleNamespace(
            policy=SafetyPolicy(), start=AsyncMock(), page=self.page, catalog_page=self.page,
            config=SimpleNamespace(root=self.root), guard_page=AsyncMock(),
        )
        self.provider = BandwagonPaymentProvider(self.browser, None)

    def locator(self, selector):
        if selector == 'input[type="password"]':
            return self.password
        if "logout.php" in selector:
            return self.logout
        if selector == "body":
            return self.body
        raise AssertionError("Unexpected selector in fixture")

    async def code(self, expected, coroutine):
        with self.assertRaises(AutoGrabError) as caught:
            await coroutine
        self.assertEqual(caught.exception.code, expected)

    async def test_phase_one_provider_retains_original_policy(self):
        browser = SimpleNamespace(policy=SafetyPolicy())
        BandwagonHostProvider(browser, None)
        self.assertIs(type(browser.policy), SafetyPolicy)
        self.assertFalse(browser.policy.permits(CONFIGURATION, "POST", "document"))

    async def test_phase_two_policy_is_installed_without_starting_browser(self):
        self.assertIsInstance(self.browser.policy, CheckoutPolicy)
        self.browser.start.assert_not_awaited()
        self.assertFalse(self.provider.boundary.authenticated_no_charge_verified)

    async def test_all_gateway_preferences_leave_real_order_submit_unavailable(self):
        for gateway in (None, "paypal", "roudiappstripecheckout", "payssionalipaycn", "unknown"):
            with self.subTest(gateway=gateway):
                provider = BandwagonPaymentProvider(self.browser, None, preferred_payment_gateway=gateway)
                await self.code("ORDER_BOUNDARY_UNVERIFIED", provider.prepare_checkout(PRODUCT, {}))
                await self.code("ORDER_BOUNDARY_UNVERIFIED", provider.submit_unpaid_order({}, PRODUCT, {}))
        self.browser.start.assert_not_awaited()
        self.page.goto.assert_not_awaited()

    async def test_boundary_boolean_cannot_enable_missing_authenticated_implementation(self):
        self.provider.boundary = SimpleNamespace(authenticated_no_charge_verified=True)
        self.browser.policy.account_reads = self.browser.policy.checkout_reads = True
        await self.code("ORDER_BOUNDARY_UNVERIFIED", self.provider.prepare_checkout(PRODUCT, {"verified": True}))
        await self.code("ORDER_BOUNDARY_UNVERIFIED", self.provider.submit_unpaid_order({"mode": "LIVE", "armed": True}, PRODUCT, {}))
        self.page.goto.assert_not_awaited()

    async def test_session_needs_real_response_and_one_visible_logout_marker(self):
        self.assertEqual(await self.provider.inspect_session(), {"status": "SESSION_VALID"})
        for count in (0, 2):
            self.logout.count.return_value = count
            self.assertEqual(await self.provider.inspect_session(), {"status": "LOGIN_REQUIRED"})
        self.logout.count.return_value = 1
        self.logout.is_visible.return_value = False
        self.assertEqual(await self.provider.inspect_session(), {"status": "LOGIN_REQUIRED"})

    async def test_password_form_means_login_required_even_with_logout_link(self):
        self.password.count.return_value = 1
        self.assertEqual(await self.provider.inspect_session(), {"status": "LOGIN_REQUIRED"})

    async def test_http_errors_and_challenge_titles_never_become_session_valid(self):
        for status, code in ((403, "HTTP_403"), (500, "NETWORK_ERROR")):
            with self.subTest(status=status):
                self.page.goto.return_value = SimpleNamespace(status=status)
                await self.code(code, self.provider.inspect_session())
        self.page.goto.return_value = SimpleNamespace(status=200)
        for title in ("Just a moment...", "Attention Required", "CAPTCHA"):
            self.page.title.return_value = title
            await self.code("CAPTCHA_REQUIRED", self.provider.inspect_session())

    async def test_session_navigation_or_selector_failure_returns_fixed_error(self):
        self.page.goto.side_effect = RuntimeError("fixture-private-account-data")
        await self.code("SESSION_UNVERIFIED", self.provider.inspect_session())

    async def test_missing_response_never_accepts_stale_logout_evidence(self):
        self.page.goto.return_value = None
        try:
            result = await self.provider.inspect_session()
        except AutoGrabError:
            return
        self.assertNotEqual(result["status"], "SESSION_VALID")

    async def test_non_success_response_never_validates_session(self):
        for status in (204, 301, 302, 304):
            with self.subTest(status=status):
                self.page.goto.return_value = SimpleNamespace(status=status)
                try:
                    result = await self.provider.inspect_session()
                except AutoGrabError:
                    continue
                self.assertNotEqual(result["status"], "SESSION_VALID")

    async def test_session_requires_exact_secure_account_page(self):
        for url in ("http://bandwagonhost.com/clientarea.php", "https://bandwagonhost.com/login.php",
                    "https://bandwagonhost.com/cart.php?a=checkout", "https://other.test/clientarea.php",
                    "https://user:fixture@bandwagonhost.com/clientarea.php"):
            with self.subTest(url=url):
                self.page.url = url
                try:
                    result = await self.provider.inspect_session()
                except AutoGrabError:
                    continue
                self.assertNotEqual(result["status"], "SESSION_VALID")

    async def test_challenge_body_cannot_be_valid_session_under_ordinary_title(self):
        self.body.inner_text.return_value = "Fixture header. Verify you are human to continue."
        await self.code("CAPTCHA_REQUIRED", self.provider.inspect_session())

    async def test_anonymous_checkout_observation_is_not_payment_ready(self):
        self.page.url = CHECKOUT
        result = await self.provider.checkout_observation(PRODUCT)
        self.assertEqual(result["status"], "LOGIN_REQUIRED")
        self.assertFalse(result["order_created"])
        self.assertFalse(result["payment_submitted"])
        self.assertEqual(result["inventory_lock"], "UNKNOWN")

    async def test_no_login_link_still_leaves_authenticated_boundary_unverified(self):
        self.page.url = CHECKOUT
        observation = observed_checkout()
        observation["loginRequired"] = False
        self.page.evaluate.return_value = observation
        result = await self.provider.checkout_observation(PRODUCT)
        self.assertEqual(result["status"], "ORDER_BOUNDARY_UNVERIFIED")
        self.assertFalse(result["order_created"])

    async def test_changed_checkout_location_form_or_submit_label_is_rejected(self):
        self.page.url = ACCOUNT
        await self.code("PROVIDER_CHANGED", self.provider.checkout_observation(PRODUCT))
        self.page.url = CHECKOUT
        for change in ({"forms": [{"method": "post", "action": "https://other.test/checkout"}]},
                       {"forms": [{"method": "get", "action": CHECKOUT}]}, {"submitLabels": ["Pay Now"]}):
            with self.subTest(change=change):
                self.page.evaluate.return_value = {**observed_checkout(), **change}
                await self.code("PROVIDER_CHANGED", self.provider.checkout_observation(PRODUCT))

    async def test_reconciliation_does_not_claim_absence_without_authenticated_evidence(self):
        self.provider.inspect_session = AsyncMock(return_value={"status": "SESSION_VALID"})
        result = await self.provider.reconcile_intent({"order_id": None, "invoice_id": None}, PRODUCT)
        self.assertEqual(result, {"status": "UNKNOWN", "reason": "AUTHENTICATED_RECONCILIATION_UNVERIFIED"})
        self.provider.inspect_session.return_value = {"status": "LOGIN_REQUIRED"}
        self.assertEqual(await self.provider.reconcile_intent({}, PRODUCT), {"status": "UNKNOWN", "reason": "LOGIN_REQUIRED"})

    def prepare_public_flow(self):
        (self.root / "data").mkdir(exist_ok=True)
        self.marker = self.root / "data/public-checkout-action.json"
        self.configuration = {
            "product_name": PRODUCT.name, "configuration_url": CONFIGURATION,
            "billing": "annually", "selected_price": "$49.99 Annually",
        }
        self.browser.configuration_evidence = AsyncMock(return_value=deepcopy(self.configuration))
        self.page.url = CONFIGURATION
        self.page.wait_for_url = AsyncMock()
        self.add_button = SimpleNamespace(
            evaluate=AsyncMock(return_value={"method": "post", "action": CONFIGURATION}), click=AsyncMock(),
        )
        self.checkout_button = SimpleNamespace(
            count=AsyncMock(return_value=1), is_enabled=AsyncMock(return_value=True),
            get_attribute=AsyncMock(return_value="window.location='cart.php?a=checkout'"), click=AsyncMock(),
        )
        self.product_marker = SimpleNamespace(count=AsyncMock(return_value=1))
        self.heading = SimpleNamespace(count=AsyncMock(return_value=1))
        self.admitted = []

        def role(role, *, name, exact):
            if role == "button" and name == "Add to Cart":
                return self.add_button
            if role == "button" and name == "Checkout":
                return self.checkout_button
            if role == "heading" and name in {"Order Summary", "Checkout"}:
                return self.heading
            raise AssertionError("Unexpected role in public fixture")

        def locator(selector):
            if selector == "strong":
                return SimpleNamespace(filter=Mock(return_value=self.product_marker))
            return self.locator(selector)

        async def add_click():
            # The durable action marker must already exist at dispatch time.
            self.assertEqual(json.loads(self.marker.read_text()), {"state": "DISPATCHED", "product_id": "44"})
            self.assertTrue(self.browser.policy.permits(CONFIGURATION, "POST", "document"))
            self.admitted.append(("POST", CONFIGURATION))
            self.page.url = "https://bandwagonhost.com/cart.php?a=view"

        async def checkout_click():
            self.assertEqual(json.loads(self.marker.read_text())["state"], "CART_CONFIRMED")
            self.assertTrue(self.browser.policy.permits(CHECKOUT, "GET", "document"))
            self.admitted.append(("GET", CHECKOUT))
            self.page.url = CHECKOUT

        self.page.get_by_role = Mock(side_effect=role)
        self.page.locator = Mock(side_effect=locator)
        self.add_button.click.side_effect = add_click
        self.checkout_button.click.side_effect = checkout_click

    async def test_public_flow_persists_before_configuration_post_and_stops_at_checkout(self):
        self.prepare_public_flow()
        result = await self.provider.inspect_public_checkout(PRODUCT, self.configuration)
        self.assertEqual(result["status"], "LOGIN_REQUIRED")
        self.assertEqual(self.admitted, [("POST", CONFIGURATION), ("GET", CHECKOUT)])
        self.assertEqual(json.loads(self.marker.read_text()), {"state": "CART_CONFIRMED", "product_id": "44"})
        self.assertFalse(self.browser.policy.permits(CHECKOUT, "POST", "document"))

    async def test_uncertain_configuration_post_never_replays_after_restart(self):
        self.prepare_public_flow()
        self.add_button.click.side_effect = AutoGrabError("ACTION_OUTCOME_UNKNOWN")
        await self.code("ACTION_OUTCOME_UNKNOWN", self.provider.inspect_public_checkout(PRODUCT, self.configuration))
        self.assertEqual(json.loads(self.marker.read_text())["state"], "DISPATCHED")
        restarted = BandwagonPaymentProvider(self.browser, None)
        await self.code("CART_REVIEW_REQUIRED", restarted.inspect_public_checkout(PRODUCT, self.configuration))
        self.add_button.click.assert_awaited_once()
        self.checkout_button.click.assert_not_awaited()

    async def test_changed_configuration_fails_before_mutation_or_marker(self):
        self.prepare_public_flow()
        self.browser.configuration_evidence.return_value["selected_price"] = "$50.00 Annually"
        await self.code("UI_CHANGED", self.provider.inspect_public_checkout(PRODUCT, self.configuration))
        self.add_button.click.assert_not_awaited()
        self.assertFalse(self.marker.exists())

    async def test_configuration_action_change_cannot_be_armed(self):
        self.prepare_public_flow()
        self.add_button.evaluate.return_value = {"method": "post", "action": CHECKOUT}
        await self.code("PROVIDER_CHANGED", self.provider.inspect_public_checkout(PRODUCT, self.configuration))
        self.add_button.click.assert_not_awaited()
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.browser.policy.permits(CHECKOUT, "POST", "document"))

    async def test_cart_mismatch_keeps_uncertain_marker_and_never_enters_checkout(self):
        self.prepare_public_flow()
        self.product_marker.count.return_value = 0
        await self.code("PRODUCT_MISMATCH", self.provider.inspect_public_checkout(PRODUCT, self.configuration))
        self.assertEqual(json.loads(self.marker.read_text())["state"], "DISPATCHED")
        self.checkout_button.click.assert_not_awaited()

    async def test_unknown_checkout_navigation_is_not_clicked(self):
        self.prepare_public_flow()
        self.checkout_button.get_attribute.return_value = "submitAndChargeCard()"
        await self.code("PROVIDER_CHANGED", self.provider.inspect_public_checkout(PRODUCT, self.configuration))
        self.checkout_button.click.assert_not_awaited()
        self.assertEqual(json.loads(self.marker.read_text())["state"], "CART_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
