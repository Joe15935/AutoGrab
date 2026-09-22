"""Offline contract tests; they do not establish real DMIT cart/stock access."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from autograb.core.errors import AutoGrabError
from autograb.core.events import EventLog
from autograb.providers.dmit import CATALOG_URL, DMITProvider, parse_catalog, parse_public_snapshot
from autograb.storage.database import Store

HTML = (Path(__file__).parent / "fixtures/dmit-standard-contract.html").read_text()


class DMITParserTests(unittest.TestCase):
    def test_observed_custom_theme_includes_locally_filtered_cards_and_stock_gate(self):
        html = (Path(__file__).parent / "fixtures/dmit-public-theme.html").read_text()
        products = parse_catalog(html)
        self.assertEqual([(p.product_id, p.availability) for p in products], [("265", "AVAILABLE"), ("58", "SOLD_OUT")])
        self.assertEqual(products[0].prices[0]["cents"], 3990)
        self.assertEqual(products[0].categories, ["gid:8"])
        self.assertTrue(all(p.order_url is None for p in products))
        missing_price = html.replace("$ 39.90", "Unknown")
        self.assertEqual(parse_catalog(missing_price)[0].availability, "UNKNOWN")

    def test_public_snapshot_does_not_accept_missing_stock_flag_or_duplicate_pid(self):
        row = {"pid": "265", "name": "HKG.AS3.Pro.TINY", "gid": "8", "stock": "", "disabled": False, "price": "$ 39.90 USD / Monthly"}
        self.assertEqual(parse_public_snapshot([row])[0].availability, "AVAILABLE")
        for rows in [[{**row, "disabled": None}], [row, row], [{**row, "extra": "unapproved"}]]:
            with self.assertRaises(AutoGrabError):
                parse_public_snapshot(rows)

    def test_explicit_stock_identity_prices_and_no_price_filter(self):
        products = parse_catalog(HTML)
        self.assertEqual([p.product_id for p in products], ["901", "902", "903"])
        self.assertEqual([p.availability for p in products], ["AVAILABLE", "SOLD_OUT", "UNKNOWN"])
        self.assertTrue(all(p.provider == "dmit" and p.eligible for p in products))
        self.assertEqual(products[0].prices, [{"cents": 99900, "currency": "USD", "period": "annually", "available": True}])
        self.assertEqual(products[2].prices[0]["currency"], None)
        self.assertEqual(products[0].product_url, CATALOG_URL)
        self.assertEqual(products[0].order_url, CATALOG_URL + "?a=add&pid=901&language=english")

    def test_order_link_is_not_stock_and_conflicting_stock_is_unknown(self):
        no_quantity = HTML.replace("3 Available", "Availability unknown")
        self.assertEqual(parse_catalog(no_quantity)[0].availability, "UNKNOWN")
        conflicting = HTML.replace("3 Available</span>", '3 Available</span><span class="stock">Out of Stock</span>')
        self.assertEqual(parse_catalog(conflicting)[0].availability, "UNKNOWN")

    def test_challenge_unrecognized_and_foreign_or_mismatched_identity_fail_closed(self):
        cases = [
            ("<title>Just a moment...</title>", "HUMAN_CHALLENGE_REQUIRED"),
            ("<form><input name='password'></form>", "DATA_SOURCE_UNAVAILABLE"),
            (HTML.replace("cart.php?a=add&amp;pid=901", "https://example.com/cart.php?a=add&amp;pid=901"), "CATALOG_INVALID_ORDER_URL"),
            (HTML.replace("pid=901", "pid=999"), "CATALOG_INVALID_ORDER_URL"),
            (HTML.replace('id="product902"', 'id="product901"'), "DATA_SOURCE_UNAVAILABLE"),
        ]
        for html, code in cases:
            with self.subTest(code=code), self.assertRaises(AutoGrabError) as error:
                parse_catalog(html)
            self.assertEqual(error.exception.code, code)

    def test_baseline_regular_and_promo_suppress_then_restock_and_new_product(self):
        products = parse_catalog(HTML)
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory) / "state.sqlite3") as store:
            self.assertEqual(store.ingest(products)["events"], [])
            self.assertEqual(store.ingest(products)["events"], [])
            recovered = [products[0], replace(products[1], availability="AVAILABLE"), products[2]]
            self.assertEqual([e["event_type"] for e in store.ingest(recovered)["events"]], ["RESTOCK"])
            self.assertEqual(store.ingest(recovered)["events"], [])
            new = replace(products[0], product_id="904", name="Example New VPS", order_url=CATALOG_URL + "?a=add&pid=904")
            self.assertEqual([e["event_type"] for e in store.ingest(recovered + [new])["events"]], ["NEW_PRODUCT"])


class DMITProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.provider = DMITProvider()
        self.provider._http = Mock(return_value=(200, HTML))

    async def test_discovery_follows_only_observed_catalog_groups_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            self.provider.log = EventLog(Path(directory) / "events.jsonl", provider="dmit")
            products = await self.provider.discover_products()
        self.assertEqual(len(products), 3)
        self.assertEqual([call.args[0] for call in self.provider._http.call_args_list], [CATALOG_URL, CATALOG_URL + "?gid=7"])
        self.assertEqual(products[0].categories, ["gid:7"])
        self.assertEqual(self.provider.source, "PUBLIC_HTTP")

    async def test_challenge_rate_limit_or_partial_scan_preserves_prior_snapshot(self):
        previous = parse_catalog(HTML)
        for responses, code in [([(403, "Forbidden")], "HUMAN_CHALLENGE_REQUIRED"),
                                ([(429, "Slow down")], "RATE_LIMITED"),
                                ([(200, HTML), (403, "Forbidden")], "HUMAN_CHALLENGE_REQUIRED")]:
            self.provider.last_products = previous
            self.provider._http.reset_mock(side_effect=True)
            self.provider._http.side_effect = responses
            with self.subTest(code=code), self.assertRaises(AutoGrabError) as error:
                await self.provider.discover_products()
            self.assertEqual(error.exception.code, code)
            self.assertEqual(self.provider.last_products, previous)
            self.assertEqual(self.provider._http.call_count, len(responses))

    async def test_stock_unknown_and_sold_out_are_distinct(self):
        self.assertEqual(await self.provider.check_stock("903"), "UNKNOWN")
        for pid, code in [("902", "OUT_OF_STOCK"), ("903", "STOCK_UNKNOWN"), ("999", "PRODUCT_MISSING")]:
            with self.subTest(pid=pid), self.assertRaises(AutoGrabError) as error:
                await self.provider.check_product(pid)
            self.assertEqual(error.exception.code, code)
        self.assertEqual((await self.provider.check_product("901")).product_id, "901")

    async def test_python_actions_require_edge_without_network_or_browser(self):
        product = parse_catalog(HTML)[0]
        for action in [self.provider.open_product(product), self.provider.prepare_cart(product), self.provider.dry_run_checkout(product, {})]:
            with self.assertRaises(AutoGrabError) as error:
                await action
            self.assertEqual(error.exception.code, "EDGE_COMPANION_REQUIRED")
        self.provider._http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
