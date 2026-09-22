"""Durable request admission; all provider traffic here is mocked."""
from concurrent.futures import ProcessPoolExecutor
import copy
from email.utils import formatdate
from io import BytesIO
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from autograb.core.rate_budget import BudgetWait, ProviderRateBudget, PublicHTTPError, retry_after_seconds
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import ProtocolError, make_message
from autograb.providers.apple import AppleProvider
from autograb.providers.apple_catalog import AppleCatalog, public_html
from autograb.providers.vmiss import VMISSProvider, CATALOG_URL
from autograb.storage.database import Store

FIXTURES = Path(__file__).parent / "fixtures"
VMISS = (FIXTURES / "vmiss-contract.html").read_text()
PICKUP = json.loads((FIXTURES / "apple-pickup.public.json").read_text())
SKU = "MYEV3CH/A"
SETTINGS = {"region":"cn", "targets":[{"sku":SKU, "stores":["R359"]}]}


def _worker_claim(path, now):
    with Store(path) as store:
        budget = ProviderRateBudget(store, clock=lambda:now)
        try:
            budget.claim("vmiss", "global", "catalog", interval_seconds=900)
            return True
        except BudgetWait:
            return False


class RateBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "autograb.sqlite3")
        self.addCleanup(self.store.close)
        self.now = [1_800_000_000.0]
        self.budget = ProviderRateBudget(self.store, clock=lambda:self.now[0])

    def claim(self, **kwargs):
        return self.budget.claim("vmiss", "global", "catalog", interval_seconds=900, **kwargs)

    def test_retry_after_date_seconds_and_malformed_values(self):
        now = self.now[0]
        self.assertEqual(retry_after_seconds("120", now), 120)
        self.assertEqual(retry_after_seconds(formatdate(now+3600, usegmt=True), now), 3600)
        self.assertEqual(retry_after_seconds(formatdate(now-60, usegmt=True), now), 0)
        for value in (None, "-1", "1.5", "invalid", "x"*129, "１２", "120\r\n"):
            self.assertIsNone(retry_after_seconds(value, now))

    def test_restart_honors_header_and_second_limit_escalates(self):
        first = self.claim()
        self.assertTrue(self.budget.failure(first, "RATE_LIMITED", retry_after="1200", limited=True))
        other = ProviderRateBudget(self.store, clock=lambda:self.now[0])
        self.now[0] += 1199
        with self.assertRaises(BudgetWait):
            other.claim("vmiss", "global", "catalog", interval_seconds=900)
        self.now[0] += 1
        second = other.claim("vmiss", "global", "catalog", interval_seconds=900)
        self.assertTrue(second.probe)
        self.assertFalse(other.success(first))
        other.failure(second, "RATE_LIMITED", retry_after="0", limited=True)
        state = other.status("vmiss", "global", "catalog")
        self.assertEqual(state["consecutive_limits"], 2)
        self.assertEqual(state["wait_seconds"], 1800)
        self.assertEqual(state["last_error"], "RATE_LIMITED")
        self.now[0] += 1800
        third = self.claim()
        self.assertTrue(self.budget.success(third))
        self.assertEqual(self.budget.status("vmiss", "global", "catalog")["consecutive_limits"], 0)

    def test_expired_lease_waits_before_one_recovery_probe_and_stale_reply_fails(self):
        first = self.claim(lease_seconds=30)
        self.now[0] += 31
        self.assertFalse(self.budget.success(first))
        with self.assertRaises(BudgetWait) as raised:
            self.claim()
        self.assertEqual(raised.exception.budget_status["last_error"], "PROBE_INTERRUPTED")
        self.assertEqual(raised.exception.budget_status["wait_seconds"], 900)
        self.now[0] += 900
        second = self.claim()
        self.assertFalse(self.budget.failure(first, "RATE_LIMITED", limited=True))
        self.assertTrue(self.budget.success(second))

    def test_three_processes_share_exactly_one_expired_cooldown_claim(self):
        self.budget.failure(self.claim(), "RATE_LIMITED", limited=True)
        self.now[0] += 900
        with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as pool:
            claims = list(pool.map(_worker_claim, [self.store.path]*3, [self.now[0]]*3))
        self.assertEqual(sum(claims), 1)

    def test_scopes_independent_and_raw_values_never_persist(self):
        ticket = self.claim()
        self.budget.failure(ticket, "RATE_LIMITED", retry_after="synthetic-untrusted-header", limited=True)
        for scope in (("apple","cn","pickup"), ("apple","us","pickup"), ("apple","cn","catalog")):
            self.assertTrue(self.budget.success(self.budget.claim(*scope, interval_seconds=60)))
        with self.assertRaises(ValueError):
            self.budget.claim("vmiss", "global", "https://example.test/private", interval_seconds=900)
        data = "\n".join(self.store.connection.iterdump())
        self.assertNotIn("synthetic-untrusted-header", data)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM provider_rate_budgets").fetchone()[0], 4)

    def test_schema_downgrade_only_applies_to_same_completed_http_response(self):
        ticket = self.claim()
        self.budget.success(ticket)
        self.assertTrue(self.budget.failure(ticket, "DATA_UNVERIFIED"))
        self.assertFalse(self.budget.failure(ticket, "DATA_UNVERIFIED"))
        self.now[0] += 900
        newer = self.claim()
        self.assertFalse(self.budget.failure(ticket, "DATA_UNVERIFIED"))
        self.assertTrue(self.budget.success(newer))

    def _browser_block(self, provider, kind, code):
        broker = EdgeBroker(self.store)
        broker.handle_event(make_message("EDGE_READY", payload={"version":"0.5.0"}))
        product_id, url = (("us-los-angeles-bgp/basic", "https://app.vmiss.com/store/us-los-angeles-bgp") if provider == "vmiss" else
                           ("cn:"+SKU, "https://www.apple.com.cn/shop/buy-iphone/iphone-16/myev3ch/a"))
        product = Product(product_id, "Synthetic browser block", "UNKNOWN", [], url, provider=provider, eligible=True)
        event = self.store.create_event(product, "PRODUCT_CHANGED", simulated=False)
        intent = broker.intents.create(event, product)
        command = broker.enqueue("OPEN_PRODUCT", intent["intent_id"], product_id,
            {"mode":"DRY_RUN", "product":{"name":product.name,"url":url,"period":"unknown","cents":None,"currency":None}})
        self.assertEqual(broker.next_command()["command_id"], command["command_id"])
        message = make_message(kind, provider=provider, intent_id=intent["intent_id"], product_id=product_id,
            command_id=command["command_id"], payload={"tab_id":7,"stage":"UNKNOWN","code":code,"challenge":"NONE","login":"UNKNOWN"})
        return broker, message

    def test_correlated_vmiss_browser_limit_revokes_http_ticket_and_survives_replay(self):
        ticket = self.claim()
        broker, message = self._browser_block("vmiss", "FAILED", "RATE_LIMITED")
        with patch("autograb.core.rate_budget.time.time", return_value=self.now[0]):
            broker.handle_event(message)
        self.assertFalse(self.budget.success(ticket))
        state = self.budget.status("vmiss", "global", "catalog")
        self.assertEqual((state["wait_seconds"], state["consecutive_limits"], state["retry_after"]), (900, 1, None))
        with self.assertRaises(BudgetWait):self.claim()
        with self.assertRaises(ProtocolError):broker.handle_event(message)
        unknown = make_message("SITE_CHANGED", provider="vmiss", intent_id=message["intent_id"], product_id=message["product_id"],
            payload=message["payload"])
        with self.assertRaises(ProtocolError):broker.handle_event(unknown)
        self.assertEqual(self.budget.status("vmiss", "global", "catalog")["consecutive_limits"], 1)
        again = make_message("SITE_CHANGED", provider="vmiss", intent_id=message["intent_id"], product_id=message["product_id"],
            command_id=message["command_id"], payload=message["payload"])
        with patch("autograb.core.rate_budget.time.time", return_value=self.now[0]):
            broker.handle_event(again)
        self.assertEqual(self.budget.status("vmiss", "global", "catalog")["wait_seconds"], 1800)
        broker.disconnect()
        disconnected = make_message("FAILED", provider="vmiss", intent_id=message["intent_id"], product_id=message["product_id"],
            command_id=message["command_id"], payload=message["payload"])
        with self.assertRaises(ProtocolError):broker.handle_event(disconnected)
        self.assertEqual(self.budget.status("vmiss", "global", "catalog")["consecutive_limits"], 2)

    def test_apple_browser_bag_block_is_only_same_region_fulfillment(self):
        ticket = self.budget.claim("apple", "cn", "fulfillment", interval_seconds=60)
        broker, message = self._browser_block("apple", "SITE_CHANGED", "APPLE_BAG_BLOCKED")
        with patch("autograb.core.rate_budget.time.time", return_value=self.now[0]):
            broker.handle_event(message)
        self.assertFalse(self.budget.success(ticket))
        self.assertEqual(self.budget.status("apple", "cn", "fulfillment")["wait_seconds"], 900)
        for scope in (("apple","cn","pickup"), ("apple","cn","catalog"), ("apple","us","fulfillment"), ("vmiss","global","catalog")):
            self.assertTrue(self.budget.success(self.budget.claim(*scope, interval_seconds=60)))

    def test_browser_block_uses_caller_transaction_and_preserves_longer_server_deadline(self):
        self.budget.failure(self.claim(), "RATE_LIMITED", retry_after="7200", limited=True)
        with self.assertRaisesRegex(ValueError, "active transaction"):
            ProviderRateBudget.record_browser_block(self.store, "vmiss", "global", "catalog", "RATE_LIMITED")
        with patch("autograb.core.rate_budget.time.time", return_value=self.now[0]):
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with self.store._transaction():
                    ProviderRateBudget.record_browser_block(self.store, "vmiss", "global", "catalog", "RATE_LIMITED")
                    raise RuntimeError("rollback")
            unchanged = self.budget.status("vmiss", "global", "catalog")
            self.assertEqual((unchanged["consecutive_limits"], unchanged["retry_after"]), (1, 7200))
            with self.store._transaction():
                ProviderRateBudget.record_browser_block(self.store, "vmiss", "global", "catalog", "RATE_LIMITED")
        changed = self.budget.status("vmiss", "global", "catalog")
        self.assertEqual((changed["consecutive_limits"], changed["retry_after"], changed["wait_seconds"]), (2, None, 7200))


class BudgetedProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "autograb.sqlite3")
        self.addCleanup(self.store.close)
        self.now = [1_800_000_000.0]
        self.budget = ProviderRateBudget(self.store, clock=lambda:self.now[0])

    async def test_vmiss_one_page_per_slot_never_ingests_partial_or_refetches_each_product(self):
        provider = VMISSProvider(budget=self.budget, clock=lambda:self.now[0])
        provider._http = Mock(side_effect=['<a href="/store/us-los-angeles-bgp">LA</a>', VMISS])
        with self.assertRaisesRegex(AutoGrabError, "CATALOG_INCOMPLETE"):
            await provider.discover_products()
        self.assertEqual(provider.last_products, [])
        self.assertEqual(self.store.list_products(), [])
        with self.assertRaises(BudgetWait):
            await provider.discover_products()
        self.assertEqual(provider._http.call_count, 1)
        self.now[0] += 900
        products = await provider.discover_products()
        self.assertEqual(len(products), 2)
        for product in products:
            self.assertEqual(await provider.get_product_details(product.product_id), product)
        self.assertEqual(provider._http.call_count, 2)

    async def test_vmiss_1015_header_survives_http_classification_and_restart(self):
        html = (FIXTURES / "vmiss-rate-limited.public.html").read_bytes()
        error = HTTPError(CATALOG_URL, 403, "blocked", {"Retry-After":formatdate(self.now[0]+3600, usegmt=True)}, BytesIO(html))
        first = VMISSProvider(budget=self.budget)
        with patch("autograb.providers.vmiss.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(AutoGrabError, "RATE_LIMITED"):
                await first.discover_products()
        restarted = VMISSProvider(budget=self.budget)
        restarted._http = Mock(side_effect=AssertionError("cooldown network request"))
        self.now[0] += 3599
        with self.assertRaises(BudgetWait):
            await restarted.discover_products()
        self.assertEqual(first.rate_status["consecutive_limits"], 1)
        self.assertEqual(restarted._http.call_count, 0)

    async def test_vmiss_failure_discards_pending_catalogue_and_old_stock_is_unknown(self):
        group = "https://app.vmiss.com/store/us-los-angeles-bgp"
        provider = VMISSProvider(budget=self.budget, clock=lambda:self.now[0])
        provider._http = Mock(side_effect=[VMISS, PublicHTTPError("RATE_LIMITED"), VMISS,
            '<a href="'+group+'">Current group</a>'])
        with self.assertRaisesRegex(AutoGrabError, "CATALOG_INCOMPLETE"):
            await provider.discover_products()
        self.now[0] += 900
        with self.assertRaisesRegex(AutoGrabError, "RATE_LIMITED"):
            await provider.discover_products()
        self.assertEqual(provider.last_products, [])
        self.now[0] += 900
        with self.assertRaisesRegex(AutoGrabError, "CATALOG_INCOMPLETE"):
            await provider.discover_products()
        self.assertEqual(provider._http.call_args.args[0], CATALOG_URL)
        self.now[0] += 900
        products = await provider.discover_products()
        self.assertTrue(all(p.availability == "UNKNOWN" for p in products))

    async def test_apple_persistent_541_cooldown_one_probe_and_repeated_limit(self):
        request = Mock(return_value=(541, None, None))
        first = AppleProvider(SETTINGS, budget=self.budget, transport=request)
        self.assertEqual((await first.discover_products())[0].availability, "UNKNOWN")
        restarted = AppleProvider(SETTINGS, budget=self.budget, transport=request)
        self.now[0] += 899
        self.assertEqual((await restarted.discover_products())[0].availability, "UNKNOWN")
        self.assertEqual(request.call_count, 1)
        self.now[0] += 1
        self.assertEqual((await restarted.discover_products())[0].availability, "UNKNOWN")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(restarted.rate_status["consecutive_limits"], 2)
        self.assertEqual(restarted.rate_status["wait_seconds"], 1800)

    async def test_apple_batch_and_regions_independent_but_each_failure_unknown(self):
        settings, pickup = copy.deepcopy(SETTINGS), copy.deepcopy(PICKUP)
        other = "TESTSKU/A"
        settings["targets"].append({"sku":other, "stores":["R359"]})
        pickup["body"]["stores"][0]["partsAvailability"][other] = copy.deepcopy(pickup["body"]["stores"][0]["partsAvailability"][SKU])
        pickup["body"]["stores"][0]["partsAvailability"][other]["partNumber"] = other
        request = Mock(return_value=(200, pickup, None))
        provider = AppleProvider(settings, budget=self.budget, transport=request)
        self.assertTrue(all(p.availability == "AVAILABLE" for p in await provider.discover_products()))
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual([query["parts.0"][0], query["parts.1"][0]], [SKU, other])
        self.assertEqual(request.call_count, 1)
        self.assertTrue(all(p.availability == "UNKNOWN" for p in await provider.discover_products()))
        us = AppleProvider({**SETTINGS,"region":"us"}, budget=self.budget, transport=request)
        await us.discover_products()
        self.assertEqual(request.call_count, 2)
        for response in ((429,None,"1800"), (None,None,None), (200,{},None)):
            self.now[0] += 3600
            request.return_value = response
            self.assertTrue(all(p.availability == "UNKNOWN" for p in await provider.discover_products()))

    def test_apple_catalog_http_header_schema_failure_and_endpoint_isolation(self):
        blocked = HTTPError("https://www.apple.com.cn/store", 541, "blocked", {"Retry-After":"2400"}, None)
        with patch("autograb.providers.apple_catalog.build_opener") as opener:
            opener.return_value.open.side_effect = blocked
            with self.assertRaisesRegex(AutoGrabError, "HTTP_BLOCKED"):
                public_html("https://www.apple.com.cn/store", budget=self.budget, region="cn")
            with self.assertRaises(BudgetWait):
                public_html("https://www.apple.com.cn/store", budget=self.budget, region="cn")
            self.assertEqual(opener.return_value.open.call_count, 1)
        self.assertEqual(self.budget.status("apple","cn","catalog")["wait_seconds"], 2400)
        self.budget.success(self.budget.claim("apple","cn","pickup",interval_seconds=60))
        self.budget.success(self.budget.claim("apple","cn","storelist",interval_seconds=60))
        self.now[0] += 2400
        catalog = AppleCatalog("cn", transport=lambda _:"<h1>Unknown public layout</h1>", budget=self.budget)
        with self.assertRaisesRegex(AutoGrabError, "CATEGORIES_UNVERIFIED"):
            catalog.categories()
        self.assertEqual(self.budget.status("apple","cn","catalog")["last_error"], "APPLE_CATALOG_CATEGORIES_UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
