import unittest
import json
from dataclasses import replace
from autograb.core.models import Product
from autograb.storage.database import Store


class OpportunityTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.product = Product("87", "SPECIAL PROMO ANNUAL", "AVAILABLE",
            [{"period": "annually", "cents": 99999999, "currency": "USD"}],
            "https://bandwagonhost.com/order/ecommerce", categories=["Regular"])

    def test_regular_words_price_and_unknown_never_execute(self):
        self.assertEqual(self.store.ingest([self.product])["events"], [])
        expensive = replace(self.product, prices=[{"period": "annually", "cents": 999999999, "currency": "USD"}])
        event = self.store.ingest([expensive])["events"][0]
        self.assertEqual(event["details"]["opportunities"], [])
        self.assertFalse(event["details"]["execution_candidate"])
        changed = replace(expensive, availability="UNKNOWN", categories=["New campaign"])
        event = self.store.ingest([changed])["events"][0]
        self.assertIn("NEW_PRODUCT_GROUP", event["details"]["opportunities"])
        self.assertFalse(event["details"]["execution_candidate"])

    def test_real_new_group_restock_and_same_id_other_provider(self):
        self.store.ingest([replace(self.product, availability="SOLD_OUT")])
        event = self.store.ingest([self.product])["events"][0]
        self.assertEqual(event["details"]["opportunities"], ["RESTOCK"])
        self.assertTrue(event["details"]["execution_candidate"])
        self.assertEqual(self.store.ingest([replace(self.product, provider="dmit")])["events"], [])
        new = replace(self.product, product_id="88", categories=["Anniversary"])
        event = self.store.ingest([self.product, new])["events"][0]
        self.assertEqual(set(event["details"]["opportunities"]), {"NEW_PRODUCT", "NEW_ANNUAL_SKU", "NEW_PRODUCT_GROUP"})
        self.assertEqual(self.store.ingest([self.product, new])["events"], [])

    def test_store_restock_survives_unknown_while_other_store_available(self):
        def product(second, fresh=True):
            entries = [{"sku": "TEST/A", "region": "cn", "mode": "pickup", "store_id": "R001", "availability": "AVAILABLE"},
                       {"sku": "TEST/A", "region": "cn", "mode": "pickup", "store_id": "R002", "availability": second, "fresh": fresh}]
            return replace(self.product, provider="apple", product_id="cn:TEST/A", metadata={"inventory": entries})
        self.store.ingest([product("SOLD_OUT")])
        self.store.ingest([product("UNKNOWN", False)])
        event = self.store.ingest([product("AVAILABLE")])["events"][0]
        self.assertIn("PICKUP_AVAILABLE", event["details"]["opportunities"])
        self.assertEqual(self.store.ingest([product("AVAILABLE")])["events"], [])

    def test_legacy_metadata_and_apple_poll_status_do_not_create_events(self):
        self.store.ingest([self.product])
        legacy = self.product.to_dict(); legacy.pop("metadata")
        self.store.connection.execute("UPDATE products SET product_json=?", (json.dumps(legacy),))
        self.assertEqual(self.store.ingest([self.product])["events"], [])
        item = {"sku":"TEST/A", "region":"cn", "mode":"pickup", "store_id":"R001", "availability":"SOLD_OUT", "fresh":True, "status":"OBSERVED"}
        apple = replace(self.product, provider="apple", product_id="cn:TEST/A", availability="SOLD_OUT",
                        metadata={"inventory":[item], "inventory_status":"OBSERVED"})
        self.store.ingest([apple])
        stale = replace(apple, availability="UNKNOWN", metadata={"inventory":[{**item,"availability":"UNKNOWN","fresh":False,"status":"STALE"}], "inventory_status":"RATE_LIMIT_WAIT"})
        self.assertEqual(self.store.ingest([stale])["events"], [])
        self.assertEqual(self.store.ingest([apple])["events"], [])

    def test_new_or_stale_apple_store_is_not_a_restock(self):
        def apple(entries, state):
            return replace(self.product, provider="apple", product_id="cn:TEST/A", availability=state, metadata={"inventory":entries})
        first = {"sku":"TEST/A", "region":"cn", "mode":"pickup", "store_id":"R001", "availability":"SOLD_OUT", "fresh":True}
        self.store.ingest([apple([first], "SOLD_OUT")])
        second = {**first, "store_id":"R002", "availability":"AVAILABLE"}
        event = self.store.ingest([apple([first, second], "AVAILABLE")])["events"][0]
        self.assertNotEqual(event["event_type"], "RESTOCK")
        self.assertFalse(event["details"]["opportunities"])
        self.assertFalse(event["details"]["execution_candidate"])
        stale = apple([{**first,"availability":"AVAILABLE","fresh":False},second], "AVAILABLE")
        self.assertEqual(self.store.ingest([stale])["events"], [])
        event = self.store.ingest([apple([{**first,"availability":"AVAILABLE"},second], "AVAILABLE")])["events"][0]
        self.assertEqual(event["details"]["opportunities"], ["PICKUP_AVAILABLE"])
        self.assertTrue(event["details"]["execution_candidate"])

    def test_disabled_annual_quote_does_not_create_an_annual_opportunity(self):
        monthly = replace(self.product, prices=[{"period":"monthly","cents":100,"currency":"USD","available":True}])
        self.store.ingest([monthly])
        disabled = replace(monthly, prices=monthly.prices + [{"period":"annually","cents":1000,"currency":"USD","available":False}])
        event = self.store.ingest([disabled])["events"][0]
        self.assertEqual(event["details"]["opportunities"], [])
        self.assertFalse(event["details"]["execution_candidate"])

    def test_new_configured_apple_sku_is_baselined_but_later_store_restock_triggers(self):
        item = {"sku":"OLD/A", "region":"cn", "mode":"pickup", "store_id":"R001", "availability":"AVAILABLE", "fresh":True}
        old = replace(self.product,provider="apple",product_id="cn:OLD/A",
            metadata={"configured_target":True,"catalog_discovered":False,"inventory":[item]})
        self.store.ingest([old])
        added = replace(old,product_id="cn:EXISTING/A",categories=["Newly configured family"],
            order_url="https://www.apple.com.cn/shop/buy-iphone/iphone-16",
            metadata={"configured_target":True,"catalog_discovered":False,"inventory":[{**item,"sku":"EXISTING/A"}]})
        event = self.store.ingest([old,added])["events"][0]
        self.assertEqual(event["details"]["opportunities"],[])
        self.assertTrue(event["details"]["regular"])
        self.assertFalse(event["details"]["execution_candidate"])
        sold = replace(added,availability="SOLD_OUT",metadata={**added.metadata,"inventory":[{**added.metadata["inventory"][0],"availability":"SOLD_OUT"}]})
        self.store.ingest([old,sold])
        event = self.store.ingest([old,added])["events"][0]
        self.assertEqual(event["details"]["opportunities"],["PICKUP_AVAILABLE"])
        self.assertTrue(event["details"]["execution_candidate"])
