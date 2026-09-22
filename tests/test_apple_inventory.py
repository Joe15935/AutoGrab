"""Small contract checks; real Apple HTTP evidence is recorded separately."""
import copy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit

from autograb.core.errors import AutoGrabError
from autograb.providers.apple import AppleInventoryMonitor, AppleProvider, parse_pickup

SKU = "MYEV3CH/A"
STORE = "R359"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "apple-pickup.public.json").read_text())
SETTINGS = {"region": "cn", "targets": [{"sku": SKU, "stores": [STORE],
    "product_url": "https://www.apple.com.cn/shop/buy-iphone/iphone-16/myev3ch/a"}]}


class AppleInventoryTests(unittest.IsolatedAsyncioTestCase):
    def test_actual_public_response_and_unknown_enum(self):
        result = parse_pickup(FIXTURE, SKU, STORE)
        self.assertEqual(result.availability, "AVAILABLE")
        for value, expected in [("unavailable", "SOLD_OUT"), ("ineligible", "UNKNOWN"), ("mystery", "UNKNOWN"), ([], "UNKNOWN")]:
            payload = copy.deepcopy(FIXTURE)
            payload["body"]["stores"][0]["partsAvailability"][SKU]["pickupDisplay"] = value
            self.assertEqual(parse_pickup(payload, SKU, STORE).availability, expected)

    def test_malformed_or_other_identity_never_becomes_stock(self):
        cases = [None, {}, {"head": []}, {**FIXTURE, "head": {"status": 541}}]
        for mutate in (
            lambda p: p["body"].update(errorMessage="unavailable"),
            lambda p: p["body"]["stores"].append(copy.deepcopy(p["body"]["stores"][0])),
            lambda p: p["body"]["stores"][0].update(storeNumber="R000"),
            lambda p: p["body"]["stores"][0]["partsAvailability"][SKU].update(partNumber="OTHER/A"),
            lambda p: p["body"]["stores"][0]["partsAvailability"][SKU].update(storePickEligible=False),
        ):
            payload = copy.deepcopy(FIXTURE); mutate(payload); cases.append(payload)
        for payload in cases:
            self.assertEqual(parse_pickup(payload, SKU, STORE).availability, "UNKNOWN")

    async def test_unconfigured_and_foreign_url_do_not_request(self):
        def forbidden(_):
            self.fail("invalid configuration must never issue HTTP")
        for settings in ({}, {"region": "cn", "targets": []}, {**SETTINGS, "targets": [{"sku": SKU, "stores": []}]},
                         {**SETTINGS, "targets": [{**SETTINGS["targets"][0], "product_url": "https://evil.example/shop/buy-iphone"}]}):
            monitor = AppleInventoryMonitor(settings, transport=forbidden)
            self.assertEqual(await monitor.poll(), [])
            self.assertIn(monitor.status, ("APPLE_TARGETS_NOT_CONFIGURED", "APPLE_CONFIG_INVALID"))

    async def test_rate_limit_and_stale_observation_cannot_reappear_available(self):
        now, calls = [100.0], []
        def request(url):
            calls.append(url)
            return 200, FIXTURE, None
        provider = AppleProvider(SETTINGS, transport=request, clock=lambda: now[0])
        product = (await provider.discover_products())[0]
        self.assertEqual((product.provider, product.product_id, product.availability), ("apple", "cn:" + SKU, "AVAILABLE"))
        self.assertEqual(product.prices, [])
        self.assertTrue(product.metadata["configured_target"])
        self.assertFalse(product.metadata["catalog_discovered"])
        self.assertIsNone(product.order_url)
        self.assertEqual(parse_qs(urlsplit(calls[0]).query)["parts.0"], [SKU])
        self.assertEqual((await provider.discover_products())[0].availability, "UNKNOWN")
        self.assertFalse(provider.inventory[0]["fresh"])
        self.assertEqual(len(calls), 1)
        now[0] += 60
        self.assertEqual((await provider.discover_products())[0].availability, "AVAILABLE")
        self.assertEqual(len(calls), 2)

    async def test_blocking_and_retry_after_respected_without_retry_loop(self):
        now = [100.0]
        for status, advance, expected_calls in [(541, 1000, 1), (403, 1000, 1), (429, 301, 1)]:
            calls = []
            def request(url):
                calls.append(url)
                return status, None, "600"
            monitor = AppleInventoryMonitor(SETTINGS, transport=request, clock=lambda: now[0])
            self.assertEqual((await monitor.poll())[0]["availability"], "UNKNOWN")
            now[0] += advance
            await monitor.poll()
            self.assertEqual(len(calls), expected_calls)

    async def test_multistore_rotation_and_delivery_remain_separate(self):
        now, calls = [100.0], []
        settings = copy.deepcopy(SETTINGS)
        settings["targets"][0].update(stores=["R359", "R360"], modes=["pickup", "delivery"], location="research-only")
        def request(url):
            calls.append(url)
            return 200, FIXTURE, None
        provider = AppleProvider(settings, transport=request, clock=lambda: now[0])
        self.assertEqual((await provider.discover_products())[0].availability, "AVAILABLE")
        self.assertEqual(provider.inventory[-1]["status"], "DELIVERY_UNVERIFIED")
        now[0] += 60
        self.assertEqual((await provider.discover_products())[0].availability, "UNKNOWN")
        self.assertEqual(parse_qs(urlsplit(calls[1]).query)["store"], ["R360"])
        with self.assertRaisesRegex(AutoGrabError, "APPLE_CHECKOUT_UNVERIFIED"):
            await provider.prepare_cart(None)
