"""Synthetic catalog parser safety tests; these do not prove live-site access."""

import copy
import json
from pathlib import Path
import unittest

from autograb.providers.catalog import CATALOG_URL, CatalogError, parse_catalog


FIXTURE = Path(__file__).parent / "fixtures" / "catalog.small.json"


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text())

    def test_stock_flags_are_not_inferred_from_prices(self):
        products = parse_catalog(self.payload)
        self.assertEqual([p.availability for p in products], ["AVAILABLE", "SOLD_OUT", "UNKNOWN"])
        self.assertEqual([p.product_id for p in products], ["44", "999", "1000"])
        self.assertTrue(all(p.order_url is None for p in products))

    def test_eligibility_has_no_price_or_hardware_threshold(self):
        products = parse_catalog(self.payload)
        self.assertEqual([p.eligible for p in products], [True, True, False])
        self.assertEqual(products[1].prices[0]["cents"], 99999999)
        self.payload["products"][1].update(ram=0, cpu=0, ssd=0, transfer=0)
        self.assertTrue(parse_catalog(self.payload)[1].eligible)

    def test_promotional_monthly_product_is_eligible(self):
        self.payload["products"][2]["name"] = "LIMITED monthly product"
        self.assertTrue(parse_catalog(self.payload)[2].eligible)

    def test_promotional_category_is_eligible(self):
        self.payload["tiers"][0]["name"] = "Special Offers"
        self.assertTrue(parse_catalog(self.payload)[2].eligible)

    def test_annual_period_normalization_preserves_safe_label(self):
        self.payload["products"][2]["prices"][0]["period"] = "  Yearly   "
        product = parse_catalog(self.payload)[2]
        self.assertTrue(product.eligible)
        self.assertEqual(product.prices[0]["period"], "Yearly")

    def test_unknown_zero_and_unavailable_prices_remain_distinct(self):
        self.payload["products"][0]["prices"] = [
            {"period": "Annually", "currency": "USD"},
            {"period": "Monthly", "cents": 0, "currency": "USD"},
            {"period": "Quarterly", "cents": -1, "currency": "USD"},
        ]
        prices = parse_catalog(self.payload)[0].prices
        self.assertEqual([p["cents"] for p in prices], [None, 0, -1])
        self.assertEqual([p["available"] for p in prices], [None, True, False])
        del self.payload["products"][0]["prices"]
        self.assertEqual(parse_catalog(self.payload)[0].prices, [])

    def test_grouping_url_uses_known_city_datacenter_and_tier(self):
        products = parse_catalog(self.payload)
        self.assertEqual(products[0].product_url, "https://bandwagonhost.com/order/basic/Vancouver/CABC_1")
        self.assertEqual(products[1].product_url, "https://bandwagonhost.com/order/basic/New%20York/USNY_6")
        self.assertEqual(products[0].locations, ["Vancouver", "New York"])
        self.assertEqual(products[0].categories, ["Basic VPS"])

    def test_unknown_grouping_falls_back_without_inventing_order_url(self):
        self.payload["products"][0]["datacenters"] = {"UNKNOWN_DC": 42}
        product = parse_catalog(self.payload)[0]
        self.assertEqual(product.product_url, CATALOG_URL)
        self.assertEqual(product.locations, [])
        self.assertIsNone(product.order_url)

    def test_invalid_or_missing_root_schema_is_rejected(self):
        invalid = [None, [], {}, {"products": []}, {"products": {}}]
        for field in ["tiers", "locations", "products"]:
            missing = copy.deepcopy(self.payload)
            del missing[field]
            invalid.append(missing)
            malformed = copy.deepcopy(self.payload)
            malformed[field] = {}
            invalid.append(malformed)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(CatalogError):
                parse_catalog(payload)

    def test_provider_error_does_not_produce_snapshot(self):
        for field, value in [("error", "Denied"), ("errors", ["Denied"]), ("success", False)]:
            with self.subTest(field=field), self.assertRaises(CatalogError):
                parse_catalog({**self.payload, field: value})

    def test_duplicate_numeric_and_string_product_ids_are_rejected(self):
        self.payload["products"][1]["id"] = "44"
        with self.assertRaises(CatalogError):
            parse_catalog(self.payload)

    def test_malformed_late_product_cannot_produce_partial_snapshot(self):
        self.payload["products"].append({"id": 2000, "name": "Late", "prices": "changed schema"})
        with self.assertRaises(CatalogError):
            parse_catalog(self.payload)

    def test_ambiguous_stock_values_stop_snapshot(self):
        for value in ["false", "true", 0, 1, [], {}]:
            with self.subTest(value=value), self.assertRaises(CatalogError):
                self.payload["products"][0]["outOfStock"] = value
                parse_catalog(self.payload)

    def test_explicit_null_stock_remains_unknown(self):
        self.payload["products"][0]["outOfStock"] = None
        self.assertEqual(parse_catalog(self.payload)[0].availability, "UNKNOWN")

    def test_unsafe_product_identifiers_are_rejected(self):
        for value in [True, 44.0, -1, "0", "044", "44/../1", "44?pid=1", "４４", "44\n"]:
            with self.subTest(value=value), self.assertRaises(CatalogError):
                self.payload["products"][0]["id"] = value
                parse_catalog(self.payload)

    def test_unsafe_route_segments_are_rejected(self):
        for value in ["../basic", "https://example.com", "basic?pid=1", "basic%2Fother", "basic\\other"]:
            payload = copy.deepcopy(self.payload)
            payload["tiers"][0]["id"] = value
            with self.subTest(value=value), self.assertRaises(CatalogError):
                parse_catalog(payload)
        for value in ["../Vancouver", "..", "Vancouver%2Fother", "Vancouver?x=1", "Vancouver\n"]:
            payload = copy.deepcopy(self.payload)
            payload["locations"][0]["city"] = value
            with self.subTest(value=value), self.assertRaises(CatalogError):
                parse_catalog(payload)

    def test_malformed_datacenter_map_is_rejected(self):
        for value in [[], {"../other": 1}, {"CABC_1": True}, {"CABC_1": "1&pid=2"}]:
            with self.subTest(value=value), self.assertRaises(CatalogError):
                self.payload["products"][0]["datacenters"] = value
                parse_catalog(self.payload)

    def test_malformed_prices_are_rejected(self):
        for value in [True, "4999", 49.99, [], {}]:
            with self.subTest(value=value), self.assertRaises(CatalogError):
                self.payload["products"][0]["prices"][0]["cents"] = value
                parse_catalog(self.payload)
        for price in [{"currency": "USD/../"}, {"period": "Annually\n"}]:
            self.payload["products"][0]["prices"] = [price]
            with self.subTest(price=price), self.assertRaises(CatalogError):
                parse_catalog(self.payload)

    def test_duplicate_metadata_ids_are_rejected(self):
        for field in ["tiers", "locations"]:
            payload = copy.deepcopy(self.payload)
            payload[field].append(copy.deepcopy(payload[field][0]))
            with self.subTest(field=field), self.assertRaises(CatalogError):
                parse_catalog(payload)

    def test_saved_phase0_public_catalog_regression(self):
        """Historical public response compatibility, not a live stock check."""
        historical = json.loads(FIXTURE.with_name("catalog.phase0.public.json").read_text())
        products = parse_catalog(historical)
        self.assertEqual(len(products), 48)
        self.assertEqual(len({product.product_id for product in products}), 48)
        self.assertEqual({product.availability for product in products}, {"AVAILABLE"})
        self.assertTrue(all(product.eligible for product in products))
        self.assertTrue(all(product.product_url != CATALOG_URL for product in products))
        self.assertTrue(all(product.order_url is None for product in products))
        self.assertEqual(
            {price["period"] for product in products for price in product.prices},
            {"Annually", "Monthly", "Quarterly", "Semi-Annually"},
        )
        self.assertEqual(
            max(price["cents"] for product in products for price in product.prices),
            1898999,
        )


if __name__ == "__main__":
    unittest.main()
