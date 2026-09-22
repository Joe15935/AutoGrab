"""Network boundary tests using synthetic URLs only; no request is dispatched."""

from types import SimpleNamespace
import unittest

from autograb.browser.safety import SafetyPolicy, is_observed_add, public_url


ADD = "https://bandwagonhost.com/cart.php?a=add&pid=44&billingcycle=annually&configoption%5B10%5D=39"


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SafetyPolicy()

    def test_public_catalog_and_read_only_cart_views_are_allowed(self):
        for url in [
            "https://bandwagonhost.com/",
            "https://bandwagonhost.com/order/get-data",
            "https://bandwagonhost.com/order/basic",
            "https://bandwagonhost.com/order/ecommerce/Vancouver/CABC_6",
            "https://bandwagonhost.com/order/ecommerce-sla-elevated/Los%20Angeles/USCA_5",
            "https://bandwagonhost.com/cart.php?a=confproduct&i=0",
            "https://bandwagonhost.com/cart.php?a=view",
        ]:
            with self.subTest(url=url):
                self.assertTrue(self.policy.permits(url, "GET", "document"))

    def test_unsafe_order_and_payment_endpoints_are_blocked(self):
        for url in [
            "https://bandwagonhost.com/cart.php?a=checkout",
            "https://bandwagonhost.com/cart.php?a=complete",
            "https://bandwagonhost.com/cart.php?a=confproduct&i=0&submit=true",
            "https://bandwagonhost.com/cart.php?a=view&a=checkout",
            "https://bandwagonhost.com/viewinvoice.php?id=1",
            "https://bandwagonhost.com/creditcard.php",
            "https://bandwagonhost.com/paypal.php",
            "https://bandwagonhost.com/clientarea.php",
        ]:
            with self.subTest(url=url):
                self.assertFalse(self.policy.permits(url, "GET", "document"))

    def test_all_mutating_methods_are_blocked_outside_manual_login(self):
        for method in ["POST", "PUT", "PATCH", "DELETE", "CONNECT"]:
            for url in ["https://bandwagonhost.com/order/get-data", ADD, "https://cdn.example.com/logo.png"]:
                with self.subTest(method=method, url=url):
                    self.assertFalse(self.policy.permits(url, method, "document"))

    def test_exact_observed_cart_ticket_is_single_use(self):
        self.assertFalse(self.policy.permits(ADD, "GET", "document"))
        self.policy.arm_config_get(ADD, "44")
        self.assertFalse(self.policy.permits(ADD.replace("pid=44", "pid=45"), "GET", "document"))
        self.assertFalse(self.policy.permits(ADD + "&extra=1", "GET", "document"))
        self.assertTrue(self.policy.permits(ADD, "GET", "document"))
        self.assertFalse(self.policy.permits(ADD, "GET", "document"))

    def test_head_cannot_consume_cart_get_ticket(self):
        self.policy.arm_config_get(ADD, "44")
        self.assertFalse(self.policy.permits(ADD, "HEAD", "document"))
        self.assertTrue(self.policy.permits(ADD, "GET", "document"))

    def test_background_request_cannot_consume_cart_ticket(self):
        self.policy.arm_config_get(ADD, "44")
        for resource_type in ["fetch", "xhr", "image", "script"]:
            with self.subTest(resource_type=resource_type):
                self.assertFalse(self.policy.permits(ADD, "GET", resource_type))
        self.assertTrue(self.policy.permits(ADD, "GET", "document"))

    def test_an_unspent_ticket_cannot_be_silently_replaced(self):
        self.policy.arm_config_get(ADD, "44")
        with self.assertRaises(ValueError):
            self.policy.arm_config_get(ADD.replace("pid=44", "pid=45"), "45")
        self.assertTrue(self.policy.permits(ADD, "GET", "document"))

    def test_unverified_cart_links_cannot_arm_policy(self):
        for url in [
            ADD.replace("https:", "http:"),
            ADD.replace("bandwagonhost.com", "bandwagonhost.com.example.com"),
            ADD.replace("bandwagonhost.com", "user:secret@bandwagonhost.com"),
            ADD.replace("pid=44", "pid=45"),
            ADD + "&pid=44",
            ADD + "&a=checkout",
            ADD + "&token=private",
            ADD + "#fragment",
            ADD.replace("annually", "annually%26a%3Dcheckout"),
            ADD.replace("%5B10%5D=39", "%5B10%5D=checkout"),
            ADD.replace("a=add", "a=checkout"),
            ADD.replace("/cart.php", "/order/basic"),
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.policy.arm_config_get(url, "44")

    def test_observed_add_requires_positive_ascii_numeric_product_id(self):
        for product_id in ["", "invalid", "-1", "0", "４４", "44/../1"]:
            with self.subTest(product_id=product_id):
                self.assertFalse(is_observed_add(
                    "https://bandwagonhost.com/cart.php?a=add&pid=" + product_id,
                    product_id,
                ))

    def test_offsite_scripts_documents_and_fetches_are_blocked(self):
        for resource_type in ["document", "script", "xhr", "fetch", "other"]:
            with self.subTest(resource_type=resource_type):
                self.assertFalse(self.policy.permits("https://example.com/file.js", "GET", resource_type))
        self.assertFalse(self.policy.permits("http://bandwagonhost.com/order/basic", "GET", "document"))

    def test_normal_passive_assets_are_allowed(self):
        for url, resource_type in [
            ("https://bandwagonhost.com/assets/app.js?v=1", "script"),
            ("https://bandwagonhost.com/assets/app.css", "stylesheet"),
            ("https://cdn.example.com/logo.png", "image"),
            ("https://fonts.example.com/font.woff2", "font"),
        ]:
            with self.subTest(url=url):
                self.assertTrue(self.policy.permits(url, "GET", resource_type))

    def test_php_path_info_cannot_disguise_a_mutation_as_an_asset(self):
        for url, resource_type in [
            ("https://bandwagonhost.com/cart.php/checkout.js?a=checkout", "script"),
            ("https://bandwagonhost.com/viewinvoice.php/logo.png?id=1", "image"),
            ("https://bandwagonhost.com/cart%2ephp/checkout.js?a=checkout", "script"),
        ]:
            with self.subTest(url=url):
                self.assertFalse(self.policy.permits(url, "GET", resource_type))

    def test_encoded_path_traversal_is_blocked(self):
        for url in [
            "https://bandwagonhost.com/order/basic/%2e%2e/%2e%2e",
            "https://bandwagonhost.com/order/basic/%2f..%2fcart.php",
            "https://bandwagonhost.com/order/basic/%252e%252e/cart.php",
            "https://bandwagonhost.com/order/basic/../cart.php",
            "https://bandwagonhost.com/order/basic/Vancouver%00/CABC_1",
        ]:
            with self.subTest(url=url):
                self.assertFalse(self.policy.permits(url, "GET", "document"))

    def test_manual_login_exception_is_narrow(self):
        login = "https://bandwagonhost.com/dologin.php"
        self.assertFalse(self.policy.permits(login, "POST", "document"))
        self.policy.login_mode = True
        self.assertTrue(self.policy.permits(login, "POST", "document"))
        self.assertTrue(self.policy.permits("https://bandwagonhost.com/clientarea.php", "GET", "document"))
        for method in ["DELETE", "PUT", "PATCH"]:
            with self.subTest(method=method):
                self.assertFalse(self.policy.permits(login, method, "document"))
        self.assertFalse(self.policy.permits(login + "?a=checkout", "POST", "document"))
        self.assertFalse(self.policy.permits(login, "POST", "fetch"))
        self.assertFalse(self.policy.permits("https://bandwagonhost.com/cart.php?a=checkout", "POST", "document"))

    def test_malformed_urls_fail_closed(self):
        for url in ["https://[invalid", "https://[::1", "https://bandwagonhost.com:bad/", "not-a-url", "https://bandwagonhost.com/\n"]:
            with self.subTest(url=url):
                self.assertFalse(self.policy.permits(url, "GET", "document"))
                self.assertFalse(is_observed_add(url, "44"))

    def test_public_url_removes_credentials_query_and_fragment(self):
        self.assertEqual(
            public_url("https://username:password@bandwagonhost.com/cart.php?token=secret#private"),
            "https://bandwagonhost.com/cart.php",
        )

    def test_public_url_hides_unknown_hosts_and_private_paths(self):
        for url in [
            "https://example.com/account/private",
            "https://bandwagonhost.com/account/reset/private-token",
            "https://[invalid",
        ]:
            with self.subTest(url=url):
                self.assertEqual(public_url(url), "UNAVAILABLE")


class SafetyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_route_is_aborted_and_log_omits_query(self):
        class FakeRoute:
            request = SimpleNamespace(
                url="https://bandwagonhost.com/cart.php?a=checkout&password=private",
                method="POST",
                resource_type="document",
            )

            def __init__(self):
                self.aborted = None
                self.continued = False

            async def abort(self, reason):
                self.aborted = reason

            async def continue_(self):
                self.continued = True

        policy = SafetyPolicy()
        route = FakeRoute()
        await policy.route(route)
        self.assertEqual(route.aborted, "blockedbyclient")
        self.assertFalse(route.continued)
        self.assertEqual(policy.blocked, [{"method": "POST", "path": "/cart.php", "resource_type": "document"}])
        self.assertNotIn("private", str(policy.blocked))

    async def test_route_diagnostics_are_bounded_and_redact_unknown_paths(self):
        class FakeRoute:
            def __init__(self, index):
                self.request = SimpleNamespace(
                    url="https://[invalid" if index == 104 else "https://bandwagonhost.com/private-token/checkout",
                    method="POST",
                    resource_type="document",
                )
                self.aborted = False

            async def abort(self, reason):
                self.aborted = reason == "blockedbyclient"

            async def continue_(self):
                raise AssertionError("Unsafe request was dispatched")

        policy = SafetyPolicy()
        for index in range(105):
            route = FakeRoute(index)
            await policy.route(route)
            self.assertTrue(route.aborted)
        self.assertEqual(len(policy.blocked), 100)
        self.assertEqual({item["path"] for item in policy.blocked}, {"[redacted-path]"})
        self.assertNotIn("private-token", str(policy.blocked))


if __name__ == "__main__":
    unittest.main()
