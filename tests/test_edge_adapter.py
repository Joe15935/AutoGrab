"""Real DOM execution against synthetic offline fixtures, never the user's Edge.

Every request is fulfilled/aborted locally; authenticated merchant HTML is not
captured. These tests prove adapter refusal/dispatch rules, not live selectors.
"""
from pathlib import Path
import unittest

from playwright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "edge-extension/providers/bandwagon/adapter.js"
FIXTURES = ROOT / "tests/fixtures"
ORIGIN = "https://bandwagonhost.com"
PRODUCT = ORIGIN + "/order/ecommerce/Los%20Angeles/USCA_9"
CONFIG = ORIGIN + "/cart.php?a=confproduct&i=0"
CART = ORIGIN + "/cart.php?a=view"
CHECKOUT = ORIGIN + "/cart.php?a=checkout"
EXPECTED = dict(product_id="87", name="SPECIAL 20G KVM PROMO V5 - CN2 GIA ECOMMERCE",
                url=PRODUCT, period="annually", cents=16999, currency="USD")


class EdgeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(service_workers="block")
        self.requests = []
        self.routes = {}

        async def offline(route):
            request = route.request
            self.requests.append((request.method, request.url))
            content = self.routes.get(request.url)
            if content is None:
                await route.abort()
            else:
                await route.fulfill(status=200, content_type="text/html", body=content)

        await self.context.route("**/*", offline)
        self.page = await self.context.new_page()
        await self.page.add_init_script(path=ADAPTER)

    async def asyncTearDown(self):
        await self.context.close()
        await self.browser.close()
        await self.playwright.stop()

    async def load(self, fixture, url, transform=None):
        html = (FIXTURES / f"edge-{fixture}.html").read_text()
        self.routes[url] = transform(html) if transform else html
        await self.page.goto(url)
        self.requests.clear()

    async def call(self, method, expected=True):
        value = EXPECTED if expected is True else expected
        return await self.page.evaluate("([method, expected]) => AutoGrabBandwagon[method](expected)", [method, value])

    async def reject(self, method, code, expected=True):
        result = await self.call(method, expected)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["code"], code)
        if method in {"selectBillingPeriod", "configureProduct", "addToCart", "openCheckout"}:
            if code == "ACTION_OUTCOME_UNKNOWN":
                self.assertNotIn("action", result)
            else:
                self.assertEqual(result["action"], "NONE")
        self.assertEqual(self.requests, [])
        return result

    async def test_product_link_identity_price_period_and_session(self):
        await self.load("product", PRODUCT)
        self.assertEqual((await self.call("detectPage"))["stage"], "PRODUCT")
        self.assertEqual((await self.call("detectLogin"))["login"], "VALID")
        self.assertTrue((await self.call("verifyProduct"))["ok"])
        self.assertEqual(self.requests, [])

    async def test_product_navigation_uses_only_observed_matching_link(self):
        await self.load("product", PRODUCT)
        target = ORIGIN + "/cart.php?a=add&pid=87&billingcycle=annually&configoption%5B17%5D=55"
        self.routes[target] = (FIXTURES / "edge-configuration.html").read_text()
        async with self.page.expect_navigation():
            result = await self.call("configureProduct")
        self.assertEqual(result["action"], "NAVIGATED")
        self.assertEqual(self.requests, [("GET", target)])

    async def test_wrong_pid_duplicate_pid_or_extra_action_link_never_navigates(self):
        for query in ("pid=88", "pid=87&amp;pid=87", "pid=87&amp;pay=1"):
            with self.subTest(query=query):
                await self.load("product", PRODUCT, lambda html: html.replace("pid=87", query))
                await self.reject("configureProduct", "SITE_CHANGED")

    async def test_external_product_link_never_navigates(self):
        await self.load("product", PRODUCT, lambda html: html.replace('href="/cart.php', 'href="https://other.invalid/cart.php'))
        await self.reject("configureProduct", "SITE_CHANGED")

    async def test_product_price_name_currency_and_period_mismatch(self):
        for key, value in (("cents", 17000),):
            expected = {**EXPECTED, key: value}
            await self.load("product", PRODUCT)
            await self.reject("configureProduct", "PRODUCT_MISMATCH", expected)
        await self.load("product", PRODUCT, lambda html: html.replace("USD", "CAD"))
        await self.reject("configureProduct", "PRODUCT_MISMATCH")
        await self.load("product", PRODUCT)
        await self.reject("configureProduct", "SITE_CHANGED", {**EXPECTED, "period": "biennially"})

    async def test_actual_public_shape_selects_year_once_without_login_handoff_or_network(self):
        await self.load("product-public", PRODUCT)
        self.assertEqual((await self.call("detectPage"))["login"], "UNKNOWN")
        result = await self.call("selectBillingPeriod")
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "CONFIGURED")
        verified = await self.call("verifyProduct")
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["code"], "PUBLIC_PRODUCT_VERIFIED")
        self.assertEqual(verified["login"], "UNKNOWN")
        self.assertEqual((await self.call("selectBillingPeriod"))["action"], "NONE")
        self.assertEqual(await self.page.evaluate("window.fixtureBillingSelections"), 1)
        self.assertEqual(self.requests, [])

    async def test_public_name_currency_are_deferred_until_strict_configuration(self):
        await self.load("product-public", PRODUCT)
        await self.call("selectBillingPeriod")
        wrong_name = {**EXPECTED, "name": "Different persisted product"}
        verified = await self.call("verifyProduct", wrong_name)
        self.assertEqual(verified["code"], "PUBLIC_PRODUCT_VERIFIED")
        await self.load("configuration", CONFIG)
        await self.reject("addToCart", "PRODUCT_MISMATCH", wrong_name)

    async def test_public_selection_rejects_wrong_pid_and_radio_inside_form(self):
        await self.load("product-public", PRODUCT, lambda html: html.replace("pid=87", "pid=88"))
        await self.reject("selectBillingPeriod", "SITE_CHANGED")
        self.assertEqual(await self.page.evaluate("window.fixtureBillingSelections"), 0)
        await self.load("product-public", PRODUCT, lambda html: html.replace('<section>', '<section><form>').replace('</section>', '</form></section>'))
        await self.reject("selectBillingPeriod", "BILLING_SELECTOR_CHANGED")
        self.assertEqual(await self.page.evaluate("window.fixtureBillingSelections"), 0)

    async def test_public_selected_view_wrong_price_never_navigates(self):
        await self.load("product-public", PRODUCT)
        await self.call("selectBillingPeriod")
        await self.page.evaluate("document.querySelector('.price').textContent = '$179.99 * /year'")
        await self.reject("configureProduct", "PRODUCT_MISMATCH")

    async def test_anonymous_public_view_can_open_verified_link_without_cart_post(self):
        await self.load("product-public", PRODUCT)
        await self.call("selectBillingPeriod")
        target = ORIGIN + "/cart.php?a=add&pid=87&billingcycle=annually&configoption%5B17%5D=146"
        self.routes[target] = (FIXTURES / "edge-configuration.html").read_text()
        async with self.page.expect_navigation():
            result = await self.call("configureProduct")
        self.assertTrue(result["ok"])
        self.assertEqual(result["login"], "UNKNOWN")
        self.assertEqual(self.requests, [("GET", target)])

    async def test_configuration_verifies_exact_name_price_billing_and_not_root_password(self):
        await self.load("configuration", CONFIG)
        result = await self.call("verifyProduct")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "CONFIGURATION")
        self.assertEqual(result["login"], "VALID")
        self.assertEqual(result["cart_id"], "configuration_0")
        self.assertEqual((await self.call("configureProduct"))["action"], "NONE")
        self.assertEqual(self.requests, [])

    async def test_configuration_wrong_price_currency_billing_or_pid_refused(self):
        cases = ((lambda html: html.replace("169.99", "170.00"), "PRICE_MISMATCH"),
                 (lambda html: html.replace("USD", "CAD"), "PRICE_MISMATCH"),
                 (lambda html: html.replace('value="annually" selected', 'value="monthly" selected'), "BILLING_MISMATCH"),
                 (lambda html: html.replace("<select", '<input name="pid" value="88"><select'), "PRODUCT_MISMATCH"))
        for transform, code in cases:
            await self.load("configuration", CONFIG, transform)
            await self.reject("addToCart", code)

    async def test_configuration_wrong_name_never_posts(self):
        await self.load("configuration", CONFIG, lambda html: html.replace(EXPECTED["name"], "Different product"))
        await self.reject("addToCart", "PRODUCT_MISMATCH")

    async def test_nonzero_configuration_index_is_readable_but_never_posted(self):
        for index in (1, 9):
            url = CONFIG.replace("i=0", f"i={index}")
            await self.load("configuration", url, lambda html: html.replace("i=0", f"i={index}"))
            verified = await self.call("verifyProduct")
            self.assertTrue(verified["ok"])
            self.assertEqual(verified["cart_id"], f"configuration_{index}")
            result = await self.reject("addToCart", "CART_REVIEW_REQUIRED")
            self.assertEqual(result["cart_id"], f"configuration_{index}")

    async def test_configuration_rechecks_form_action_and_override(self):
        for replacement in ('action="/cart.php?a=checkout"', 'action="https://other.invalid/cart.php"', 'action="/cart.php?a=confproduct&amp;i=1"'):
            await self.load("configuration", CONFIG, lambda html: html.replace('action="/cart.php?a=confproduct&amp;i=0"', replacement))
            await self.reject("addToCart", "UNSAFE_FORM_ACTION")
        await self.load("configuration", CONFIG, lambda html: html.replace('type="submit"', 'type="submit" formaction="/cart.php?a=checkout"'))
        await self.reject("addToCart", "UNSAFE_FORM_ACTION")

    async def test_one_configuration_post_and_never_final_checkout_post(self):
        await self.load("configuration", CONFIG)
        self.routes[CONFIG] = (FIXTURES / "edge-cart.html").read_text()
        async with self.page.expect_navigation():
            result = await self.call("addToCart")
        self.assertEqual(result["action"], "SUBMITTED")
        self.assertEqual(self.requests, [("POST", CONFIG)])

    async def test_same_document_repeat_post_rejected_even_without_navigation(self):
        await self.load("configuration", CONFIG)
        await self.page.evaluate("document.forms[0].addEventListener('submit', event => event.preventDefault())")
        self.assertTrue((await self.call("addToCart"))["ok"])
        await self.reject("addToCart", "ACTION_OUTCOME_UNKNOWN")

    async def test_reinjection_preserves_dispatch_tombstones(self):
        await self.load("configuration", CONFIG)
        await self.page.evaluate("document.forms[0].addEventListener('submit', event => event.preventDefault())")
        self.assertTrue((await self.call("addToCart"))["ok"])
        await self.page.evaluate(ADAPTER.read_text())
        await self.reject("addToCart", "ACTION_OUTCOME_UNKNOWN")

    async def test_explicit_sold_out_product_does_not_navigate(self):
        await self.load("product", PRODUCT, lambda html: html.replace("Order Now", "Sold Out"))
        await self.reject("configureProduct", "SOLD_OUT")

    async def test_cart_match_and_checkout_navigation_are_distinct(self):
        await self.load("cart", CART)
        self.assertEqual((await self.call("verifyCart"))["code"], "CART_READY")
        self.routes[CHECKOUT] = (FIXTURES / "edge-checkout.html").read_text()
        async with self.page.expect_navigation():
            result = await self.call("openCheckout")
        self.assertEqual(result["action"], "NAVIGATED")
        self.assertEqual(self.requests, [("GET", CHECKOUT)])
        self.assertEqual((await self.call("verifyCheckout"))["code"], "CHECKOUT_READY")

    async def test_observed_cart_following_edit_row_and_recurring_summary_verify_read_only(self):
        await self.load("cart-observed", CART)
        for _ in range(2):
            result = await self.call("verifyCart")
            self.assertTrue(result["ok"])
            self.assertEqual(result["code"], "CART_READY")
            self.assertEqual(result["cart_id"], "configuration_0")
        self.assertEqual(self.requests, [])

    async def test_observed_cart_wrong_recurring_period_or_amount_is_rejected(self):
        for transform in (lambda html: html.replace("Annually", "Monthly"),
                          lambda html: html.replace("$169.99 USD Annually", "$179.99 USD Annually"),
                          lambda html: html.replace("$169.99 USD Annually", "$169.99 CAD Annually"),
                          lambda html: html.replace("$169.99 USD</td>", "$169.99 USD Monthly</td>", 1),
                          lambda html: html.replace("Total Recurring", "Unverified Recurring")):
            await self.load("cart-observed", CART, transform)
            await self.reject("verifyCart", "CART_REVIEW_REQUIRED")

    async def test_observed_cart_rejects_second_item_and_unrelated_edit_row(self):
        cases = (lambda html: html.replace("</tbody>", '<tr><td>Other product</td><td>$10.00 USD</td></tr></tbody>'),
                 lambda html: html.replace("</tbody>", '<tr><td>Other product</td><td>$10.00 CAD</td></tr></tbody>'),
                 lambda html: html.replace("</table>", '</table><table><tr><td>Other product</td><td>$10.00 USD</td></tr></table>'),
                 lambda html: html.replace('<tr><td colspan="2">', '<tr><td>Unexpected row</td></tr><tr><td colspan="2">'),
                 lambda html: html.replace("[<a href=", 'Unverified service [<a href='))
        for transform in cases:
            await self.load("cart-observed", CART, transform)
            await self.reject("verifyCart", "CART_REVIEW_REQUIRED")

    async def test_unknown_cart_wrong_product_or_additional_item_stops(self):
        cases = (lambda html: html.replace(EXPECTED["name"], "Another product"),
                 lambda html: html.replace("$169.99 USD Annually", "$179.99 USD Annually"),
                 lambda html: html.replace("Annually", "Monthly"),
                 lambda html: html.replace("</tbody>", '<tr><td><strong>Other service</strong></td><td>$10.00 USD</td></tr></tbody>'),
                 lambda html: html.replace("</tbody>", '<tr><td><strong>VPS - Self-managed - Other</strong></td></tr></tbody>'),
                 lambda html: html.replace("<a href=\"/cart.php", '<input name="qty" value="2"><a href="/cart.php'))
        for transform in cases:
            await self.load("cart", CART, transform)
            await self.reject("openCheckout", "CART_REVIEW_REQUIRED")

    async def test_missing_or_duplicate_edit_configuration_link_stops_cart(self):
        await self.load("cart", CART, lambda html: html.replace("confproduct", "unknown"))
        await self.reject("verifyCart", "CART_REVIEW_REQUIRED")
        await self.load("cart", CART, lambda html: html.replace("Edit Configuration</a>", 'Edit Configuration</a><a href="/cart.php?a=confproduct&amp;i=1">Extra</a>'))
        await self.reject("verifyCart", "CART_REVIEW_REQUIRED")

    async def test_modified_checkout_click_script_or_submit_button_stops(self):
        for transform, code in ((lambda html: html.replace("window.location='cart.php?a=checkout'", "submitAndPay()"), "CHECKOUT_TARGET_CHANGED"),
                                (lambda html: html.replace('type="button"', 'type="submit"'), "UNSAFE_CHECKOUT_TYPE")):
            await self.load("cart", CART, transform)
            await self.reject("openCheckout", code)

    async def test_input_button_checkout_dispatches_only_fixed_official_get(self):
        for statement in ("window.location='cart.php?a=checkout'", "  window.location = 'cart.php?a=checkout' ;  "):
            await self.load("cart-input-checkout", CART, lambda html: html.replace("window.location='cart.php?a=checkout'", statement))
            self.routes[CHECKOUT] = (FIXTURES / "edge-checkout.html").read_text()
            # A separate click listener must not be invoked: the adapter verifies
            # the literal but performs its own fixed GET, never click/eval.
            await self.page.evaluate("document.querySelector('[value=Checkout]').addEventListener('click', () => location.assign('https://other.invalid/'))")
            async with self.page.expect_navigation():
                result = await self.call("openCheckout")
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "NAVIGATED")
            self.assertEqual(self.requests, [("GET", CHECKOUT)])

    async def test_input_checkout_missing_duplicate_submit_or_changed_target_is_refused(self):
        cases = ((lambda html: html.replace('value="Checkout"', 'value="Pay Now"'), "CHECKOUT_CONTROL_MISSING"),
                 (lambda html: html.replace('</body>', '<input type="button" value="Checkout"></body>'), "CHECKOUT_CONTROL_MISSING"),
                 (lambda html: html.replace('type="button"', 'type="submit"'), "UNSAFE_CHECKOUT_TYPE"),
                 (lambda html: html.replace("cart.php?a=checkout'", "cart.php?a=complete'"), "CHECKOUT_TARGET_CHANGED"),
                 (lambda html: html.replace("cart.php?a=checkout'", "https://other.invalid/cart.php?a=checkout'"), "CHECKOUT_TARGET_CHANGED"),
                 (lambda html: html.replace("cart.php?a=checkout'", "cart.php?a=checkout'; submitOrder()"), "CHECKOUT_TARGET_CHANGED"))
        for transform, code in cases:
            await self.load("cart-input-checkout", CART, transform)
            await self.reject("openCheckout", code)

    async def test_checkout_read_never_checks_terms_changes_gateway_or_submits(self):
        await self.load("checkout", CHECKOUT)
        self.assertTrue((await self.call("verifyCheckout"))["ok"])
        self.assertFalse(await self.page.locator('[name="accepttos"]').is_checked())
        self.assertTrue(await self.page.locator('[name="paymentmethod"]').is_checked())
        await self.reject("detectPaymentReady", "PAYMENT_READY_UNVERIFIED")
        await self.reject("detectOrder", "ORDER_UNVERIFIED")

    async def test_anonymous_checkout_returns_login_required_not_ready(self):
        await self.load("checkout", CHECKOUT, lambda html: html.replace('<a href="/logout.php">Logout</a>', '<a href="/cart.php?a=login">Login</a>'))
        result = await self.reject("verifyCheckout", "LOGIN_REQUIRED")
        self.assertEqual(result["login"], "REQUIRED")

    async def test_only_explicit_zero_cart_with_disabled_checkout_allows_rebuild_evidence(self):
        # Minimal reconstruction of the real empty cart seen before this sprint.
        empty = '''<h1>Order Summary</h1><table>
        <tr><th>Description</th><th>Price</th></tr>
        <tr><td colspan="2">Your Shopping Cart is Empty</td></tr>
        <tr><td>Subtotal:</td><td>$0.00 USD</td></tr>
        <tr><td>Total Due Today:</td><td>$0.00 USD</td></tr></table>
        <input type="button" disabled value="Checkout">'''
        for html, is_empty in (
            (empty, True),
            (empty.replace("Your Shopping Cart is Empty", "Loading"), False),
            (empty.replace(" disabled", ""), False),
            (empty.replace("$0.00", "$169.99"), False),
            (empty.replace("</table>", '<tr><td>Another product</td><td>$0.00 USD</td></tr></table>'), False),
            (empty + '<a href="/cart.php?a=confproduct&i=0">Edit Configuration</a>', False),
        ):
            with self.subTest(html=html):
                self.routes[CART] = html
                await self.page.goto(CART)
                self.requests.clear()
                result = await self.call("verifyCart")
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"] == "CART_EMPTY", is_empty)
                self.assertEqual(self.requests, [])

    async def test_explicit_login_page_blocks_cart_actions(self):
        await self.load("login", ORIGIN + "/clientarea.php")
        self.assertEqual((await self.call("detectPage"))["stage"], "LOGIN")
        self.assertEqual((await self.call("detectLogin"))["login"], "REQUIRED")
        await self.reject("addToCart", "LOGIN_REQUIRED")

    async def test_duplicate_logout_marker_is_not_valid_session(self):
        await self.load("configuration", CONFIG, lambda html: html.replace("</nav>", '<a href="/logout.php">Logout</a></nav>'))
        result = await self.reject("detectLogin", "LOGIN_REQUIRED")
        self.assertEqual(result["login"], "UNKNOWN")

    async def test_public_configuration_unknown_login_can_submit_exact_dry_cart_once(self):
        await self.load("configuration", CONFIG, lambda html: html.replace('<a href="/logout.php">Logout</a>', '<a href="/register.php">Register</a><a href="/clientarea.php">Client Area</a>'))
        configured = await self.call("configureProduct")
        self.assertTrue(configured["ok"])
        self.assertEqual(configured["action"], "NONE")
        self.assertEqual(configured["login"], "UNKNOWN")
        await self.page.evaluate("document.forms[0].addEventListener('submit', event => { event.preventDefault(); window.fixtureSubmits = (window.fixtureSubmits || 0) + 1; })")
        submitted = await self.call("addToCart")
        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["login"], "UNKNOWN")
        self.assertEqual(submitted["action"], "SUBMITTED")
        await self.reject("addToCart", "ACTION_OUTCOME_UNKNOWN")
        self.assertEqual(await self.page.evaluate("window.fixtureSubmits"), 1)

    async def test_public_unknown_login_configuration_posts_only_its_verified_form(self):
        await self.load("configuration", CONFIG, lambda html: html.replace('<a href="/logout.php">Logout</a>', '<a href="/clientarea.php">Client Area</a>'))
        self.routes[CONFIG] = (FIXTURES / "edge-cart.html").read_text()
        async with self.page.expect_navigation():
            result = await self.call("addToCart")
        self.assertEqual(result["action"], "SUBMITTED")
        self.assertEqual(result["login"], "UNKNOWN")
        self.assertEqual(self.requests, [("POST", CONFIG)])

    async def test_public_cart_unknown_login_can_navigate_but_checkout_requires_valid_session(self):
        await self.load("cart", CART, lambda html: html.replace('<a href="/logout.php">Logout</a>', '<a href="/clientarea.php">Client Area</a>'))
        self.routes[CHECKOUT] = (FIXTURES / "edge-checkout.html").read_text().replace('<a href="/logout.php">Logout</a>', '<a href="/clientarea.php">Client Area</a>')
        async with self.page.expect_navigation():
            result = await self.call("openCheckout")
        self.assertEqual(result["action"], "NAVIGATED")
        self.assertEqual(result["login"], "UNKNOWN")
        self.assertEqual(self.requests, [("GET", CHECKOUT)])
        self.requests.clear()
        await self.reject("verifyCheckout", "LOGIN_REQUIRED")
        self.assertFalse(await self.page.locator('[name="accepttos"]').is_checked())

    async def test_explicit_login_prompt_on_configuration_or_cart_blocks_all_mutation(self):
        for fixture, url, operation in (("configuration", CONFIG, "addToCart"), ("cart", CART, "openCheckout")):
            await self.load(fixture, url, lambda html: html.replace('<a href="/logout.php">Logout</a>', '<a href="/cart.php?a=login">Already Registered? Login</a>'))
            result = await self.reject(operation, "LOGIN_REQUIRED")
            self.assertEqual(result["login"], "REQUIRED")

    async def test_every_method_pauses_before_action_on_challenge(self):
        await self.load("challenge", CONFIG)
        methods = ["detectPage", "detectChallenge", "detectLogin", "verifyProduct", "selectBillingPeriod", "configureProduct", "addToCart", "verifyCart", "openCheckout", "verifyCheckout", "detectOrder", "detectInvoice", "detectPaymentReady"]
        for method in methods:
            result = await self.reject(method, "HUMAN_CHALLENGE_REQUIRED")
            self.assertEqual(result["challenge"], "REQUIRED")

    async def test_visible_challenge_marker_blocks_normal_title_and_product(self):
        await self.load("configuration", CONFIG, lambda html: html.replace("</body>", '<div class="cf-turnstile">Verification</div></body>'))
        await self.reject("addToCart", "HUMAN_CHALLENGE_REQUIRED")

    async def test_challenge_frame_is_detected_without_accessing_frame_or_session_values(self):
        await self.load("configuration", CONFIG)
        await self.page.evaluate("""() => {
            const deny = () => { throw new Error('FORBIDDEN_PRIVATE_READ'); };
            Object.defineProperty(document, 'cookie', {get: deny});
            Object.defineProperty(globalThis, 'localStorage', {get: deny});
            Object.defineProperty(globalThis, 'sessionStorage', {get: deny});
            const frame = document.createElement('iframe');
            frame.src = 'https://challenges.cloudflare.com/';
            Object.defineProperty(frame, 'contentDocument', {get: deny});
            Object.defineProperty(frame, 'contentWindow', {get: deny});
            document.body.appendChild(frame);
        }""")
        await self.page.wait_for_load_state("networkidle")
        self.requests.clear()
        await self.reject("detectChallenge", "HUMAN_CHALLENGE_REQUIRED")
        await self.reject("addToCart", "HUMAN_CHALLENGE_REQUIRED")

    async def test_normal_observations_do_not_read_cookies_storage_or_password_values(self):
        await self.load("configuration", CONFIG)
        await self.page.evaluate("""() => {
            const deny = () => { throw new Error('FORBIDDEN_PRIVATE_READ'); };
            Object.defineProperty(document, 'cookie', {get: deny});
            Object.defineProperty(globalThis, 'localStorage', {get: deny});
            Object.defineProperty(globalThis, 'sessionStorage', {get: deny});
            Object.defineProperty(document.querySelector('[name="rootpw"]'), 'value', {get: deny});
        }""")
        self.assertTrue((await self.call("detectLogin"))["ok"])
        self.assertTrue((await self.call("verifyProduct"))["ok"])
        self.assertEqual(self.requests, [])

    async def test_invoice_or_order_url_is_not_verified_receipt(self):
        await self.load("invoice", ORIGIN + "/viewinvoice.php?id=456")
        self.assertEqual((await self.call("detectPage"))["stage"], "INVOICE")
        await self.reject("detectInvoice", "INVOICE_UNVERIFIED")
        await self.reject("detectPaymentReady", "PAYMENT_READY_UNVERIFIED")
        await self.load("invoice", ORIGIN + "/cart.php?a=complete")
        self.assertEqual((await self.call("detectPage"))["stage"], "ORDER")
        await self.reject("detectOrder", "ORDER_UNVERIFIED")

    async def test_nonofficial_page_always_refuses_even_with_copied_markup(self):
        await self.load("configuration", "https://other.invalid/cart.php?a=confproduct&i=0")
        await self.reject("verifyProduct", "SITE_CHANGED")
        await self.reject("addToCart", "SITE_CHANGED")

    async def test_malformed_expected_identity_cannot_dispatch(self):
        await self.load("product", PRODUCT)
        for key, value in (("product_id", "８７"), ("product_id", 87), ("product_id", "0"), ("period", "monthly"),
                           ("currency", "EUR"), ("cents", 169.99), ("url", "https://other.invalid/order/ecommerce/test")):
            with self.subTest(key=key, value=value), self.assertRaises(Exception):
                await self.call("configureProduct", {**EXPECTED, key: value})
            self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
