"""Small public parser regressions. VMISS contract is synthetic/live unverified."""
from pathlib import Path
from io import BytesIO
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

from autograb.core.errors import AutoGrabError
from autograb.providers import vmiss, vps

FIXTURES = Path(__file__).parent / "fixtures"
VPS = (FIXTURES / "vps-public-card.html").read_text()
VMISS = (FIXTURES / "vmiss-contract.html").read_text()
FAMILY = "https://v.ps/products/cloud-kvm-vps/"


class PublicParserTests(unittest.TestCase):
    def test_real_vps_card_identity_and_price_are_not_stock_evidence(self):
        product, = vps.parse_catalog(VPS, FAMILY)
        self.assertEqual((product.provider, product.product_id, product.name), ("vps", "148", "NRT Starter"))
        self.assertEqual(product.order_url, "https://vps.hosting/?cmd=cart&action=add&id=148")
        self.assertEqual(product.availability, "UNKNOWN")
        self.assertEqual(product.prices, [{"cents": 695, "currency": "EUR", "period": "monthly", "available": True}])
        annual, = vps.parse_catalog(VPS.replace("€6.95", "€999.95").replace("/mo", "/yr"), FAMILY)
        self.assertEqual(annual.prices[0]["cents"], 99995)
        self.assertEqual(annual.prices[0]["period"], "annually")
        unpriced, = vps.parse_catalog(VPS.replace("€6.95", "Contact us"), FAMILY)
        self.assertEqual(unpriced.prices, [])
        self.assertEqual(unpriced.availability, "UNKNOWN")

    def test_vps_hidden_conflicting_and_disabled_stock_never_becomes_available(self):
        for evidence in ('<span hidden class="stock">In stock</span>',
                         '<span class="stock">Not in stock</span>',
                         '<span class="stock">In stock</span><span class="stock">Sold out</span>'):
            with self.subTest(evidence=evidence):
                product, = vps.parse_catalog(VPS.replace("</ul>", "</ul>" + evidence), FAMILY)
                self.assertEqual(product.availability, "UNKNOWN")
        product, = vps.parse_catalog(VPS.replace('class="group"', 'class="group disabled"')
                                    .replace("</ul>", '</ul><span class="stock">In stock</span>'), FAMILY)
        self.assertEqual(product.availability, "UNKNOWN")

    def test_vps_rejects_external_or_ambiguous_order_identity(self):
        for bad in (VPS.replace("vps.hosting/", "vps.hosting.evil/"),
                    VPS.replace("id=148", "id=148&amp;id=149"),
                    VPS.replace("action=add", "action=checkout"),
                    VPS + VPS):
            with self.subTest(), self.assertRaises(AutoGrabError):
                vps.parse_catalog(bad, FAMILY)

    def test_vmiss_contract_keeps_all_prices_and_requires_coherent_stock(self):
        first, second = vmiss.parse_catalog(VMISS)
        self.assertEqual(first.product_id, "us-los-angeles-bgp/basic")
        self.assertEqual((first.availability, second.availability), ("AVAILABLE", "SOLD_OUT"))
        self.assertTrue(first.eligible and second.eligible)
        self.assertEqual(second.prices[0]["cents"], 99900)
        self.assertTrue(second.prices[0]["available"])
        for html in (VMISS.replace("3 Available", "Available"),
                     VMISS.replace("3 Available", "3 Available Sold Out"),
                     VMISS.replace('class="package-qty"', 'hidden class="package-qty"')):
            with self.subTest():
                self.assertEqual(vmiss.parse_catalog(html)[0].availability, "UNKNOWN")

    def test_vmiss_rejects_bad_identity_or_challenge(self):
        for html in (VMISS.replace("/store/us-los-angeles-bgp/basic", "https://evil.example/store/x/y"),
                     "<title>Just a moment</title>", "<h1>Unrecognized layout</h1>"):
            with self.subTest(), self.assertRaises(AutoGrabError):
                vmiss.parse_catalog(html)

    def test_observed_vmiss_error_1015_is_rate_limited_not_human_challenge(self):
        html = (FIXTURES / "vmiss-rate-limited.public.html").read_text()
        with self.assertRaises(AutoGrabError) as raised:
            vmiss.parse_catalog(html)
        self.assertEqual(raised.exception.code, "RATE_LIMITED")
        for status, body, expected in ((403, html, "RATE_LIMITED"), (429, "", "RATE_LIMITED"),
                                        (403, "Forbidden", "HTTP_403"),
                                        (403, "<title>Just a moment</title>", "HUMAN_CHALLENGE_REQUIRED")):
            with self.subTest(status=status, code=expected):
                error = HTTPError(vmiss.CATALOG_URL, status, "blocked", {}, BytesIO(body.encode()))
                self.assertEqual(vmiss._http_error_code(error), expected)


class PublicDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_vps_only_fetches_public_readonly_catalog_pages(self):
        provider = vps.VPSProvider()
        provider._http = Mock(side_effect=['<a href="/products/cloud-kvm-vps/">Cloud</a>', VPS])
        products = await provider.discover_products()
        self.assertEqual(len(products), 1)
        self.assertEqual([call.args[0] for call in provider._http.call_args_list], [vps.CATALOG_URL, FAMILY])
        self.assertEqual(provider.source, vps.CATALOG_URL)
        with self.assertRaises(AutoGrabError):
            await provider.prepare_cart(products[0])

    async def test_vmiss_follows_only_observed_store_groups_and_keeps_no_partial_snapshot(self):
        provider = vmiss.VMISSProvider()
        provider._http = Mock(side_effect=['<a href="/store/us-los-angeles-bgp">LA</a>', VMISS])
        self.assertEqual(len(await provider.discover_products()), 2)
        self.assertEqual(provider._http.call_count, 2)
        fresh = vmiss.VMISSProvider()
        fresh._http = Mock(side_effect=[VMISS, AutoGrabError("HUMAN_CHALLENGE_REQUIRED")])
        with self.assertRaises(AutoGrabError):
            await fresh.discover_products()
        self.assertEqual(fresh.last_products, [])


if __name__ == "__main__":
    unittest.main()
