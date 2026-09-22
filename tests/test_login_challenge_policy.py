"""Synthetic request boundary tests; never connects to a merchant or challenge service."""

from types import SimpleNamespace
import unittest

from autograb.browser.checkout_policy import CheckoutPolicy
from autograb.browser.login_policy import HumanLoginPolicy
from autograb.browser.safety import SafetyPolicy


HOST = "https://bandwagonhost.com"
CF = "https://challenges.cloudflare.com"
CHALLENGE = "/cdn-cgi/challenge-platform/h/g/flow/fixture"
TOKEN = "fixture-challenge-value"


class HumanLoginPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = HumanLoginPolicy()

    def test_manual_login_policy_enables_only_explicit_human_mode(self):
        self.assertTrue(self.policy.login_mode)
        self.assertIsNone(self.policy.ticket)
        self.assertTrue(self.policy.permits(HOST + "/dologin.php", "POST", "document"))
        self.assertFalse(self.policy.permits(HOST + "/dologin.php", "POST", "fetch"))

    def test_exact_official_challenge_resource_gets_are_allowed(self):
        for origin in [HOST, CF]:
            for method in ["GET", "HEAD"]:
                for kind in ["document", "script", "xhr", "fetch", "other", "image", "stylesheet", "font"]:
                    with self.subTest(origin=origin, method=method, kind=kind):
                        self.assertTrue(self.policy.permits(origin + CHALLENGE, method, kind))

    def test_challenge_posts_require_xhr_or_fetch(self):
        for origin in [HOST, CF]:
            for kind in ["xhr", "fetch"]:
                with self.subTest(origin=origin, kind=kind):
                    self.assertTrue(self.policy.permits(origin + CHALLENGE, "POST", kind))
            for kind in ["document", "script", "image", "font", "other", "websocket", "manifest"]:
                with self.subTest(origin=origin, kind=kind):
                    self.assertFalse(self.policy.permits(origin + CHALLENGE, "POST", kind))

    def test_other_methods_never_use_challenge_exception(self):
        for method in ["PUT", "PATCH", "DELETE", "CONNECT", "TRACE", "OPTIONS"]:
            for origin in [HOST, CF]:
                with self.subTest(method=method, origin=origin):
                    self.assertFalse(self.policy.permits(origin + CHALLENGE, method, "fetch"))

    def test_turnstile_loader_is_an_exact_cross_origin_script_read(self):
        loader = CF + "/turnstile/v0/api.js"
        for method in ["GET", "HEAD"]:
            self.assertTrue(self.policy.permits(loader, method, "script"))
        for method, kind in [("POST", "script"), ("GET", "document"), ("GET", "fetch"), ("GET", "xhr")]:
            self.assertFalse(self.policy.permits(loader, method, kind))
        for path in ["/turnstile/v0/siteverify", "/turnstile/v0/api.js/extra", "/turnstile/v1/api.js", "/api.js"]:
            self.assertFalse(self.policy.permits(CF + path, "POST", "fetch"))
            self.assertFalse(self.policy.permits(CF + path, "GET", "script"))

    def test_versioned_turnstile_loader_accepts_only_bounded_hex_builds(self):
        for build in ["a", "abc012345678", "f" * 64]:
            for query in ["", "?render=explicit&onload=fixtureCallback"]:
                loader = CF + "/turnstile/v0/g/" + build + "/api.js" + query
                for method in ["GET", "HEAD"]:
                    with self.subTest(build=build, query=query, method=method):
                        self.assertTrue(self.policy.permits(loader, method, "script"))
                for method, kind in [("POST", "script"), ("POST", "fetch"), ("POST", "document"),
                                     ("GET", "document"), ("GET", "fetch"), ("GET", "xhr")]:
                    with self.subTest(build=build, method=method, kind=kind):
                        self.assertFalse(self.policy.permits(loader, method, kind))

    def test_versioned_turnstile_loader_rejects_unobserved_paths_hosts_and_verification_posts(self):
        for path in [
            "/turnstile/v0/g//api.js", "/turnstile/v0/g/" + "f" * 65 + "/api.js",
            "/turnstile/v0/g/nothex/api.js", "/turnstile/v0/g/ABC123/api.js",
            "/turnstile/v0/g/１２/api.js", "/turnstile/v0/g/abc012/other.js",
            "/turnstile/v0/g/abc012/api.js/extra", "/turnstile/v0/g/abc012/apiXjs",
            "/turnstile/v0/g/abc012/siteverify", "/turnstile/v0/g/abc012/token",
            "/turnstile/v0/g/abc012/../api.js", "/turnstile/v0/g/abc012/%2e%2e/api.js",
            "/turnstile/v0/g/abc012/%252e%252e/api.js", "/turnstile/v0/g/abc012%2f/api.js",
            "/turnstile/v0/%67/abc012/api.js", "/turnstile/v0/g/abc012/api%2ejs",
            "/turnstile/v0/g/abc012/api.js#fragment", "/turnstile/v0/g/abc012/cart.php/api.js",
        ]:
            with self.subTest(path=path):
                self.assertFalse(self.policy.permits(CF + path, "GET", "script"))
                self.assertFalse(self.policy.permits(CF + path, "POST", "fetch"))
        for origin in ["https://example.test", "https://challenges.cloudflare.com.evil.test",
                       "https://evil.challenges.cloudflare.com", "https://cloudflare.com",
                       "http://challenges.cloudflare.com", "https://challenges.cloudflare.com:444",
                       "https://user:secret@challenges.cloudflare.com"]:
            with self.subTest(origin=origin):
                self.assertFalse(self.policy.permits(origin + "/turnstile/v0/g/abc012345678/api.js", "GET", "script"))

    def test_observed_challenge_navigation_is_limited_to_login_pages(self):
        for path in ["/clientarea.php", "/login.php", "/dologin.php"]:
            url = HOST + path + "?__cf_chl_rt_tk=" + TOKEN
            for method in ["GET", "HEAD"]:
                self.assertTrue(self.policy.permits(url, method, "document"))
            for method, kind in [("POST", "document"), ("GET", "fetch"), ("GET", "script"), ("GET", "xhr")]:
                self.assertFalse(self.policy.permits(url, method, kind))

    def test_query_exceptions_cannot_carry_account_actions(self):
        for query in [
            "__cf_chl_rt_tk=", "__cf_chl_rt_tk", "__cf_chl_rt_tk=" + "x" * 2049,
            "__cf_chl_rt_tk=x&__cf_chl_rt_tk=y", "__cf_chl_rt_tk=x&action=cancel",
            "action=invoices", "action=cancel", "__cf_chl_rt_tk=x&invoiceid=123",
            "__cf_chl_rt_tk=x&pay=1", "token=x", "__cf_chl_rt_tk=x&", "__cf_other=x",
        ]:
            with self.subTest(query=query):
                self.assertFalse(self.policy.permits(HOST + "/clientarea.php?" + query, "GET", "document"))

    def test_challenge_token_does_not_open_order_or_payment_paths(self):
        for path in ["/cart.php", "/viewinvoice.php", "/creditcard.php", "/paypal.php", "/order.php"]:
            url = HOST + path + "?__cf_chl_rt_tk=" + TOKEN
            self.assertFalse(self.policy.permits(url, "GET", "document"))
            self.assertFalse(self.policy.permits(url, "POST", "document"))

    def test_order_and_payment_dispatch_remain_closed_in_human_mode(self):
        for path in [
            "/cart.php?a=add&pid=44", "/cart.php?a=checkout", "/cart.php?a=complete",
            "/cart.php?a=confproduct&i=0", "/viewinvoice.php?id=1", "/creditcard.php",
            "/paypal.php", "/clientarea.php?action=masspay", "/clientarea.php?action=addfunds",
        ]:
            for kind in ["document", "xhr", "fetch"]:
                with self.subTest(path=path, kind=kind):
                    self.assertFalse(self.policy.permits(HOST + path, "POST", kind))

    def test_host_transport_and_port_ambiguities_cannot_use_exception(self):
        for origin in [
            "http://bandwagonhost.com", "http://challenges.cloudflare.com",
            "https://bandwagonhost.com.evil.test", "https://challenges.cloudflare.com.evil.test",
            "https://evil.challenges.cloudflare.com", "https://cloudflare.com",
            "https://bandwagonhost.com.", "https://challenges.cloudflare.com.",
            "https://bandwagonhost.com:444", "https://challenges.cloudflare.com:444",
            "https://user:secret@bandwagonhost.com", "https://user:secret@challenges.cloudflare.com",
        ]:
            with self.subTest(origin=origin):
                self.assertFalse(self.policy.permits(origin + CHALLENGE, "POST", "fetch"))
                self.assertFalse(self.policy.permits(origin + CHALLENGE, "GET", "script"))

    def test_path_normalization_and_php_cannot_disguise_mutations(self):
        for path in [
            "/cdn-cgi/other/flow", "/cdn-cgi/challenge-platform-evil/flow",
            "/cdn-cgi/challenge-platform/../cart.php", "/cdn-cgi/challenge-platform/%2e%2e/cart.php",
            "/cdn-cgi/challenge-platform/%252e%252e/cart.php",
            "/cdn-cgi/challenge-platform/%2fcart.php", "/cdn-cgi/challenge-platform/%5ccart.php",
            "/cdn-cgi/challenge-platform//flow", "/cdn-cgi/challenge-platform/./flow",
            "/cdn-cgi/challenge-platform/cart.php/flow.js", "/cdn-cgi/challenge-platform/cart%2ephp/flow.js",
            "/cdn-cgi/challenge-platform/cart.PHTML/flow.js",
            "/cdn-cgi/challenge-platform/flow#fragment", "/cdn-cgi/challenge-platform/flow%00",
        ]:
            for origin in [HOST, CF]:
                with self.subTest(origin=origin, path=path):
                    self.assertFalse(self.policy.permits(origin + path, "POST", "fetch"))

    def test_encoded_challenge_prefix_does_not_use_literal_exception(self):
        for path in [
            "/%63dn-cgi/challenge-platform/flow", "/cdn-cgi/%63hallenge-platform/flow",
            "/cdn-cgi/challenge%2dplatform/flow",
        ]:
            for origin in [HOST, CF]:
                with self.subTest(origin=origin, path=path):
                    self.assertFalse(self.policy.permits(origin + path, "POST", "fetch"))

    def test_disabling_human_mode_removes_all_new_exceptions(self):
        self.policy.login_mode = False
        for url, method, kind in [
            (HOST + CHALLENGE, "GET", "script"), (HOST + CHALLENGE, "POST", "fetch"),
            (CF + CHALLENGE, "GET", "document"), (CF + "/turnstile/v0/api.js", "GET", "script"),
            (CF + "/turnstile/v0/g/abc012345678/api.js", "GET", "script"),
            (HOST + "/clientarea.php?__cf_chl_rt_tk=" + TOKEN, "GET", "document"),
        ]:
            self.assertFalse(self.policy.permits(url, method, kind))

    def test_automatic_policies_do_not_inherit_human_exceptions(self):
        for policy in [SafetyPolicy(), CheckoutPolicy()]:
            for url, method, kind in [
                (HOST + CHALLENGE, "POST", "fetch"), (CF + CHALLENGE, "GET", "document"),
                (CF + "/turnstile/v0/api.js", "GET", "script"),
                (CF + "/turnstile/v0/g/abc012345678/api.js", "GET", "script"),
                (HOST + "/clientarea.php?__cf_chl_rt_tk=" + TOKEN, "GET", "document"),
            ]:
                self.assertFalse(policy.permits(url, method, kind))


class HumanLoginDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_challenge_denial_keeps_query_token_and_body_out_of_diagnostics(self):
        request = SimpleNamespace(
            url=CF + CHALLENGE + "?secret_query=fixture-private-value",
            method="DELETE", resource_type="fetch", post_data="fixture-private-body",
        )
        class Route:
            def __init__(self):
                self.request = request
                self.aborted = False

            async def continue_(self):
                raise AssertionError("Unexpected network dispatch")

            async def abort(self, reason):
                self.aborted = reason == "blockedbyclient"

        policy, route = HumanLoginPolicy(), Route()
        await policy.route(route)
        self.assertTrue(route.aborted)
        self.assertEqual(policy.blocked, [{"method": "DELETE", "path": "[redacted-path]", "resource_type": "fetch"}])
        self.assertNotIn("fixture-private", str(policy.blocked))


if __name__ == "__main__":
    unittest.main()
