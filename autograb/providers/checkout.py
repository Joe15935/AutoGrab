"""Current observed public boundary, with authenticated steps failing closed.

2026-09-22: observed confproduct POST -> cart view -> checkout GET, and anonymous
Complete Order POST to cart.php?a=checkout. The authenticated balance, mandate,
saved-card, invoice and reconciliation semantics have NOT been verified. The
final submit method intentionally remains unavailable until that review.
"""
from autograb.browser.checkout_policy import CheckoutPolicy
from autograb.browser.manager import write_marker
from autograb.core.errors import AutoGrabError
from .bandwagon import BandwagonHostProvider


class BandwagonCheckoutBoundary:
    configuration = "OBSERVED_PUBLIC_CONFIGURATION_POST"
    cart = "OBSERVED_PUBLIC_CART"
    checkout = "OBSERVED_ANONYMOUS_CHECKOUT"
    order_submit_button = "Complete Order"
    order_submit_candidate = "https://bandwagonhost.com/cart.php?a=checkout"
    authenticated_no_charge_verified = False
    inventory_lock = "UNKNOWN"
    invoice_expiry = "UNKNOWN"
    public_gateways = ("paypal", "roudiappstripecheckout", "payssionunionpaycn", "payssionalipaycn")


class BandwagonPaymentProvider(BandwagonHostProvider):
    boundary = BandwagonCheckoutBoundary()

    def __init__(self, browser, log, *, preferred_payment_gateway=None):
        super().__init__(browser, log)
        # Installing this policy before browser.start keeps Phase 1 unchanged.
        self.browser.policy = CheckoutPolicy()
        self.preferred_payment_gateway = preferred_payment_gateway

    async def inspect_session(self):
        await self.browser.start()
        page = self.browser.catalog_page
        self.browser.policy.account_reads = True
        try:
            response = await page.goto("https://bandwagonhost.com/clientarea.php", wait_until="domcontentloaded")
            title = (await page.title()).casefold()
            protection_text = (await page.locator("body").inner_text()).casefold()
            if any(x in title + " " + protection_text for x in ("just a moment", "attention required", "captcha", "verify you are human", "checking your browser")):
                raise AutoGrabError("CAPTCHA_REQUIRED")
            if response and response.status == 403:
                raise AutoGrabError("HTTP_403")
            if response is None or response.status != 200:
                raise AutoGrabError("NETWORK_ERROR")
            logged_out = await page.locator('input[type="password"]').count() > 0
            logout = page.locator('a[href="logout.php"],a[href="/logout.php"]')
            valid = (not logged_out and page.url == "https://bandwagonhost.com/clientarea.php"
                     and await logout.count() == 1 and await logout.is_visible())
            # No names, account addresses, credit balance or input values copied.
            return {"status": "SESSION_VALID" if valid else "LOGIN_REQUIRED"}
        except AutoGrabError:
            raise
        except Exception:
            raise AutoGrabError("SESSION_UNVERIFIED") from None

    async def inspect_public_checkout(self, product, configuration):
        """Explicit boundary inspection only. Does not authorize order creation."""
        page = self.browser.page
        marker = self.browser.config.root / "data/public-checkout-action.json"
        if marker.exists() or marker.is_symlink():
            raise AutoGrabError("CART_REVIEW_REQUIRED")
        fresh = await self.browser.configuration_evidence(product)
        if any(fresh.get(k) != configuration.get(k) for k in ("product_name", "configuration_url", "billing", "selected_price")):
            raise AutoGrabError("UI_CHANGED")
        button = page.get_by_role("button", name="Add to Cart", exact=True)
        forms = await button.evaluate("e=>({method:e.form?.method,action:e.form?.action})")
        if forms != {"method": "post", "action": configuration["configuration_url"]}:
            raise AutoGrabError("PROVIDER_CHANGED")
        write_marker(marker, {"state": "DISPATCHED", "product_id": product.product_id})
        self.browser.policy.allow_configuration_post(forms["action"])
        await button.click()
        await page.wait_for_url("https://bandwagonhost.com/cart.php?a=view")
        await self.browser.guard_page(page)
        if await page.get_by_role("heading", name="Order Summary", exact=True).count() != 1:
            raise AutoGrabError("PROVIDER_CHANGED")
        matching = page.locator("strong").filter(has_text=product.name)
        if await matching.count() != 1:
            raise AutoGrabError("PRODUCT_MISMATCH")
        write_marker(marker, {"state": "CART_CONFIRMED", "product_id": product.product_id})
        checkout = page.get_by_role("button", name="Checkout", exact=True)
        if await checkout.count() != 1 or not await checkout.is_enabled():
            raise AutoGrabError("PROVIDER_CHANGED")
        navigation = await checkout.get_attribute("onclick")
        if navigation != "window.location='cart.php?a=checkout'":
            raise AutoGrabError("PROVIDER_CHANGED")
        self.browser.policy.checkout_reads = True
        await checkout.click()
        await page.wait_for_url("https://bandwagonhost.com/cart.php?a=checkout")
        await self.browser.guard_page(page)
        return await self.checkout_observation(product)

    async def checkout_observation(self, product):
        page = self.browser.page
        if page.url != "https://bandwagonhost.com/cart.php?a=checkout":
            raise AutoGrabError("PROVIDER_CHANGED")
        if await page.get_by_role("heading", name="Checkout", exact=True).count() != 1:
            raise AutoGrabError("PROVIDER_CHANGED")
        observation = await page.evaluate("""() => ({
          loginRequired:!!document.querySelector('a[href="/cart.php?a=login"]'),
          forms:[...document.forms].map(f=>({method:f.method,action:f.action})),
          gatewayNames:[...document.querySelectorAll('input[type=radio][name=paymentmethod]')].map(e=>e.value),
          termsRequired:!!document.querySelector('input[type=checkbox][name=accepttos]'),
          submitLabels:[...document.querySelectorAll('input[type=submit],button[type=submit]')].map(e=>e.value||e.innerText)
        })""")
        if {"method": "post", "action": self.boundary.order_submit_candidate} not in observation["forms"] or "Complete Order" not in observation["submitLabels"]:
            raise AutoGrabError("PROVIDER_CHANGED")
        return {"status": "LOGIN_REQUIRED" if observation["loginRequired"] else "ORDER_BOUNDARY_UNVERIFIED",
                "checkout_page_observed": True, "terms_required": observation["termsRequired"],
                "gateway_names_public": observation["gatewayNames"], "order_created": False,
                "payment_submitted": False, "inventory_lock": "UNKNOWN"}

    async def prepare_checkout(self, product, configuration):
        # Automated LIVE must not begin until account-specific semantics are known.
        raise AutoGrabError("ORDER_BOUNDARY_UNVERIFIED")

    async def submit_unpaid_order(self, intent, product, checkout):
        # No unverified final-click implementation is shipped behind a toggle.
        raise AutoGrabError("ORDER_BOUNDARY_UNVERIFIED")

    async def reconcile_intent(self, intent, product):
        session = await self.inspect_session()
        if session["status"] != "SESSION_VALID":
            return {"status": "UNKNOWN", "reason": "LOGIN_REQUIRED"}
        # Matching real order/invoice selectors requires the first authenticated
        # review. Absence of a locally known ID never proves server-side absence.
        return {"status": "UNKNOWN", "reason": "AUTHENTICATED_RECONCILIATION_UNVERIFIED"}
