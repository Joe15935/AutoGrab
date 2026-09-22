"""One-shot notification and pre-dispatch controls for public monitoring."""
from dataclasses import replace
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from autograb.core.errors import AutoGrabError
from autograb.core.config import Config
from autograb.core.models import Product
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import ProtocolError
from autograb.multi_cli import process_opportunity, run_multi
from autograb.notifications.email import NotificationResult
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore


class MultiDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.product = Product("87", "20G KVM - PROMO", "AVAILABLE",
            [{"period":"annually","cents":4999,"currency":"USD"}],
            "https://bandwagonhost.com/order/ecommerce", provider="bandwagon")
        self.notifier = type("Notifier", (), {"send_event":AsyncMock(return_value=NotificationResult("SMTP_ACCEPTED"))})()

    def restock(self, product):
        self.store.ingest([replace(product,availability="SOLD_OUT")])
        return self.store.ingest([product])["events"][0]

    def provider(self, product):
        return type("Provider", (), {"provider_name":product.provider, "check_product":AsyncMock(return_value=product)})()

    async def test_cross_provider_rejected_readonly_unqueued_and_email_once(self):
        product = replace(self.product,provider="dmit",product_url="https://www.dmit.io/cart.php?gid=1")
        event, provider = self.restock(product), self.provider(product)
        with self.assertRaisesRegex(AutoGrabError,"PROVIDER_IDENTITY_MISMATCH"):
            await process_opportunity(self.store,self.provider(self.product),event,self.notifier,prepare_checkout=True)
        self.assertEqual(self.store.get_event(event["id"])["status"],"PENDING")
        result = await process_opportunity(self.store,provider,event,self.notifier,prepare_checkout=True)
        self.assertEqual(result,"ADAPTER_READ_ONLY")
        provider.check_product.assert_not_awaited()
        self.assertEqual(IntentStore(self.store).list(),[])
        self.assertEqual(await process_opportunity(self.store,provider,event,self.notifier,prepare_checkout=True),"ALREADY_CONSUMED")
        self.assertEqual(self.notifier.send_event.await_count,1)

    async def test_stop_during_fresh_recheck_and_product_change_never_reserve_intent(self):
        event = self.restock(self.product)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            async def recheck(_):
                (data / "disarm.signal").touch()
                return self.product
            provider = self.provider(self.product)
            provider.check_product.side_effect = recheck
            result = await process_opportunity(self.store,provider,event,self.notifier,prepare_checkout=True,data_dir=data)
            self.assertEqual(result,"DISARMED")
            self.assertEqual(IntentStore(self.store).list(),[])
        self.store.ingest([replace(self.product,availability="SOLD_OUT")])
        second = self.store.ingest([self.product])["events"][0]
        provider = self.provider(replace(self.product,prices=[{"period":"annually","cents":5999,"currency":"USD"}]))
        self.assertEqual(await process_opportunity(self.store,provider,second,self.notifier,prepare_checkout=True),"OFFICIAL_PRODUCT_CHANGED")
        self.assertEqual(IntentStore(self.store).list(),[])

    async def test_rejected_queue_releases_only_intent_without_transport_evidence(self):
        event, provider = self.restock(self.product), self.provider(self.product)
        with patch.object(EdgeBroker,"status",return_value={"connected":True}), patch.object(EdgeBroker,"enqueue",side_effect=ProtocolError("EDGE_DISCONNECTED")):
            await process_opportunity(self.store,provider,event,self.notifier,prepare_checkout=True)
        intents = IntentStore(self.store)
        self.assertEqual(intents.get_by_event(event["id"])["state"],"PRE_SUBMIT_ABORTED")
        self.assertIsNone(intents.active_for_product("87","bandwagon"))
        # A queue operation that committed before an uncertain result must keep
        # its intent lock; it must never be made eligible for another dispatch.
        self.store.ingest([replace(self.product,availability="SOLD_OUT")])
        later = self.store.ingest([self.product])["events"][0]
        original = EdgeBroker.enqueue
        def uncertain(broker,*args,**kwargs):
            original(broker,*args,**kwargs)
            raise ProtocolError("REPLY_UNCERTAIN")
        with patch.object(EdgeBroker,"status",return_value={"connected":True}), patch.object(EdgeBroker,"enqueue",uncertain):
            await process_opportunity(self.store,provider,later,self.notifier,prepare_checkout=True)
        self.assertEqual(intents.get_by_event(later["id"])["state"],"INTENT_CREATED")
        self.assertIsNotNone(intents.active_for_product("87","bandwagon"))

    async def test_stop_during_catalog_fetch_prevents_new_batch_and_notifications(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(root=Path(directory)); config.prepare()
            async def discover():
                (config.root / "data/stop-monitoring.signal").touch()
                return [self.product]
            provider = self.provider(self.product)
            provider.discover_products = discover
            args = SimpleNamespace(command="monitor", provider="bandwagon", once=True, prepare_checkout=True)
            with patch("autograb.multi_cli.create_provider",return_value=provider), patch("autograb.multi_cli.EmailNotifier",return_value=self.notifier), redirect_stdout(StringIO()):
                self.assertEqual(await run_multi(args, config),0)
            with Store(config.root / "data/autograb.sqlite3") as stored:
                self.assertEqual(stored.summary()["known_count"],0)
                self.assertEqual(stored.list_events(),[])
            self.notifier.send_event.assert_not_awaited()

    async def test_observed_http_denial_pauses_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(root=Path(directory)); config.prepare()
            provider = self.provider(replace(self.product, provider="vmiss"))
            provider.discover_products = AsyncMock(side_effect=AutoGrabError("HTTP_403"))
            args = SimpleNamespace(command="monitor", provider="vmiss", once=False, prepare_checkout=False)
            with patch("autograb.multi_cli.create_provider", return_value=provider), patch("autograb.multi_cli.EmailNotifier", return_value=self.notifier), patch("autograb.multi_cli.asyncio.sleep", side_effect=AssertionError("blocked provider retried")), redirect_stdout(StringIO()):
                self.assertEqual(await run_multi(args, config), 2)
            self.assertEqual(provider.discover_products.await_count, 1)
            self.notifier.send_event.assert_not_awaited()
