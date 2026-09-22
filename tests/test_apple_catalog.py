"""Small regressions after current official CN catalog and wizard validation."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from autograb.apple_cli import configure, save_target
from autograb.core.config import Config
from autograb.core.errors import AutoGrabError
from autograb.notifications.email import EmailNotifier, NotificationResult, SMTPConfig
from autograb.providers.apple import AppleProvider
from autograb.providers.apple_catalog import AppleCatalog, merge_observations, parse_purchase_page, public_html
from autograb.storage.database import Store

URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-16"
HTML = (Path(__file__).parent / "fixtures/apple-catalog-iphone16.public.html").read_text()


class AppleCatalogTests(unittest.IsolatedAsyncioTestCase):
    def test_actual_variant_fields_and_storefront_boundaries(self):
        products = parse_purchase_page(HTML, URL, "cn")
        black = next(p for p in products if p.product_id == "cn:MYEV3CH/A")
        self.assertEqual(len(products), 5)
        self.assertEqual(black.metadata["catalog_variant"]["model"], "iPhone 16")
        self.assertEqual(black.metadata["catalog_variant"]["capacity"], "128 GB")
        self.assertEqual(black.metadata["catalog_variant"]["color"], "黑色")
        self.assertEqual(black.metadata["catalog_variant"]["carrier"], "NOT_APPLICABLE")
        self.assertEqual(black.prices[0]["cents"], 599900)
        self.assertEqual(black.metadata["order_state"], "UNKNOWN")
        self.assertEqual(black.product_url, URL + "/myev3ch/a")
        with self.assertRaisesRegex(AutoGrabError, "URL_REJECTED"):
            parse_purchase_page(HTML, URL, "us")
        bad_links = '<a href="http://www.apple.com/shop/buy-iphone">iPhone</a>'
        with self.assertRaisesRegex(AutoGrabError, "CATEGORIES_UNVERIFIED"):
            AppleCatalog("us", transport=lambda _: bad_links).categories()
        redirected = HTTPError("https://www.apple.com/store", 302, "", {"Location":"https://www.apple.com/hk-zh/shop/buy-iphone"}, None)
        with patch("autograb.providers.apple_catalog.build_opener") as opener:
            opener.return_value.open.side_effect = redirected
            with self.assertRaisesRegex(AutoGrabError, "REDIRECT_REJECTED"):
                public_html("https://www.apple.com/store")
            self.assertEqual(opener.return_value.open.call_count, 1)

    async def test_scope_baseline_new_sku_and_fresh_store_inventory_share_ledger(self):
        products = parse_purchase_page(HTML, URL, "cn")
        first = products[0]
        item = {"sku":first.product_id.split(":")[1], "region":"cn", "mode":"pickup", "store_id":"R359", "availability":"SOLD_OUT", "fresh":True}
        inventory = replace(first, availability="SOLD_OUT", metadata={"configured_target":True,"catalog_discovered":False,"inventory":[item],"delivery":"UNKNOWN","checkout":"UNVERIFIED"},locations=["R359"],eligible=True)
        with Store(":memory:") as store:
            store.ingest([inventory])
            snapshot = store.ingest(merge_observations([*products, inventory], store.list_products()))
            self.assertTrue(all(not e["details"]["opportunities"] for e in snapshot["events"]))
            self.assertEqual(store.ingest(merge_observations(products,store.list_products()))["events"], [])
            new = replace(first,product_id="cn:NEWTEST/A")
            event = store.ingest(merge_observations([*products,new],store.list_products()))["events"][0]
            self.assertEqual(event["details"]["opportunities"],["NEW_SKU"])
            self.assertFalse(event["details"]["execution_candidate"])
            unknown = replace(inventory,availability="UNKNOWN",metadata={**inventory.metadata,"inventory":[{**item,"availability":"UNKNOWN","fresh":False}]})
            self.assertEqual(store.ingest(merge_observations([unknown],store.list_products()))["events"], [])
            restocked = replace(inventory,availability="AVAILABLE",metadata={**inventory.metadata,"inventory":[{**item,"availability":"AVAILABLE"}]})
            event = store.ingest(merge_observations([*products,restocked],store.list_products()))["events"][0]
            self.assertEqual(event["event_type"],"RESTOCK")
            self.assertEqual(event["details"]["opportunities"],["PICKUP_AVAILABLE"])
            self.assertTrue(event["details"]["execution_candidate"])
            self.assertEqual(store.ingest(merge_observations([restocked],store.list_products()))["events"], [])
            other = replace(new,product_id="cn:OTHERTEST/A",categories=["Apple","ipad"],metadata={**new.metadata,"catalog_scope":"cn:ipad"})
            event = store.ingest(merge_observations([other],store.list_products()))["events"][0]
            self.assertEqual(event["details"]["opportunities"],[])
        pickup=json.loads((Path(__file__).parent/"fixtures/apple-pickup.public.json").read_text())
        clock=[100.0]
        settings={"region":"cn","catalog_enabled":True,"catalog_categories":["iphone"],
            "targets":[{"sku":"MYEV3CH/A","stores":["R359"],"product_url":URL+"/myev3ch/a"}]}
        with patch.object(AppleCatalog,"refresh",return_value=products) as refresh:
            provider=AppleProvider(settings,transport=lambda _:(200,pickup,None),clock=lambda:clock[0])
            combined=await provider.discover_products()
            self.assertEqual(len(combined),5)
            self.assertTrue(next(p for p in combined if p.product_id=="cn:MYEV3CH/A").metadata["inventory"][0]["fresh"])
            clock[0]+=60; await provider.discover_products()
            self.assertEqual(refresh.call_count,1)
            clock[0]+=3600; await provider.discover_products()
            self.assertEqual(refresh.call_count,2)

    async def test_wizard_preserves_main_config_and_mail_names_exact_store(self):
        class CurrentCatalog:
            def __init__(self,*args,**kwargs): pass
            def categories(self): return {"iphone":URL}
            def refresh(self,_): return parse_purchase_page(HTML,URL,"cn")
            def stores(self): return [{"store_id":"R359","name":"南京东路","city":"上海"}]
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/"config").mkdir()
            main=root/"config/config.toml"
            original=b'[providers.apple]\nenabled = false\n[smtp]\nhost = "smtp.example.test"\n'
            main.write_bytes(original)
            answers=iter(["1","1","1","1","1","y"])
            settings,products=configure(Config.load(root),region="cn",input_fn=lambda _:next(answers),output=lambda _:None,catalog_factory=CurrentCatalog)
            self.assertEqual(main.read_bytes(),original)
            self.assertEqual(Config.load(root).providers["apple"],settings)
            self.assertEqual(settings["targets"][0]["sku"],"MYEV3CH/A")
            self.assertEqual(settings["targets"][0]["stores"],["R359"])
            self.assertEqual((root/"config/apple.local.toml").stat().st_mode & 0o777,0o600)
            save_target(root,settings)
            self.assertEqual(len(list((root/"config").glob("apple.local.toml.*.bak"))),1)
        product=next(p for p in products if p.product_id=="cn:MYEV3CH/A")
        product=replace(product,metadata={**product.metadata,"inventory":[{"sku":"MYEV3CH/A","region":"cn","mode":"pickup","store_id":"R359","availability":"AVAILABLE","fresh":True}]})
        notifier=EmailNotifier(SMTPConfig.from_env({}))
        captured=[]
        with patch.object(notifier,"_configuration_result",return_value=None),patch.object(notifier,"_send",side_effect=lambda message:(captured.append(message) or NotificationResult("SMTP_ACCEPTED"))):
            await notifier.send_event(product,{"id":1,"event_type":"RESTOCK","details":{"opportunities":["PICKUP_AVAILABLE"]}}, {},{"status":"READ_ONLY"})
        body=captured[0].get_content()
        self.assertIn("MYEV3CH/A | cn | pickup | R359 | AVAILABLE | fresh",body)
        self.assertIn("PICKUP_AVAILABLE",body)
        self.assertIn("DRY RUN",body)
