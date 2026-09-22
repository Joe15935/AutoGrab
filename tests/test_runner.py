"""Offline workflow integration: real SQLite, fake provider and SMTP boundary.

No website, browser, credentials, or SMTP service is used by these tests.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from autograb.core.errors import AutoGrabError
from autograb.core.models import Product
from autograb.core.runner import Runner
from autograb.notifications.email import NotificationResult
from autograb.providers.base import BaseProvider
from autograb.storage.database import Store


def available_product():
    return Product(
        product_id="87", name="Fixture VPS", availability="AVAILABLE",
        prices=[{"period": "annually", "price": 99, "currency": "USD"}],
        product_url="https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9",
        eligible=True,
    )


class FakeProvider:
    provider_name = "bandwagon"

    def __init__(self):
        self.product = available_product()
        self.calls = []
        self.check_error = None
        self.cart_error = None
        self.cart_started = asyncio.Event()
        self.release_cart = None
        self.boundary = {
            "status": "DRY_RUN_BOUNDARY_REACHED",
            "boundary": "PRODUCT_CONFIGURATION_BEFORE_FORM_POST",
            "order_created": False, "cart_reserved": False, "payment_url": None,
            "cart_url": "https://bandwagonhost.com/cart.php?a=view",
        }

    async def discover_products(self):
        self.calls.append("discover_products")
        return [self.product]

    async def check_product(self, product_id):
        self.calls.append(("check_product", product_id))
        await asyncio.sleep(0)
        if self.check_error:
            raise self.check_error
        return self.product

    async def check_stock(self, product_id):
        return (await self.get_product_details(product_id)).availability

    async def get_product_details(self, product_id):
        return self.product

    async def open_product(self, product):
        self.calls.append(("open_product", product.product_id))

    async def prepare_cart(self, product):
        self.calls.append(("prepare_cart", product.product_id))
        self.cart_started.set()
        if self.release_cart is not None:
            await self.release_cart.wait()
        if self.cart_error:
            raise self.cart_error
        return {"configuration_url": "https://bandwagonhost.com/cart.php?a=confproduct", "reused_configuration": False}

    async def dry_run_checkout(self, product, cart):
        self.calls.append(("dry_run_checkout", product.product_id))
        return deepcopy(self.boundary)


class FakeNotifier:
    def __init__(self, result=None, error=None):
        self.result = result or NotificationResult("SMTP_ACCEPTED", detail="Fixture SMTP acceptance")
        self.error = error
        self.calls = []

    async def send_event(self, product, event, timing, boundary):
        self.calls.append(deepcopy({"product": product.to_dict(), "event": event, "timing": timing, "boundary": boundary}))
        if self.error:
            raise self.error
        return self.result


class MemoryLog:
    def __init__(self):
        self.records = []

    def write(self, kind, **fields):
        self.records.append({"kind": kind, **deepcopy(fields)})


class RunnerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.provider: BaseProvider = FakeProvider()
        self.notifier = FakeNotifier()
        self.log = MemoryLog()
        self.runner = Runner(self.store, self.provider, self.notifier, self.log)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, kind="RESTOCK", simulated=True):
        return self.store.create_event(available_product(), kind, simulated=simulated)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.runner = Runner(self.store, self.provider, self.notifier, self.log)

    async def test_baseline_discovers_without_triggering_browser_or_email(self):
        snapshot = self.store.ingest(await self.provider.discover_products(), source="OFFLINE_FIXTURE")
        for event in snapshot["events"]:
            await self.runner.process(event)
        self.assertTrue(snapshot["baseline_initialized"])
        self.assertEqual(snapshot["known_count"], 1)
        self.assertEqual(snapshot["events"], [])
        self.assertEqual(self.provider.calls, ["discover_products"])
        self.assertEqual(self.notifier.calls, [])
        self.assertEqual(self.store.summary()["event_count"], 0)

    async def test_simulated_new_and_restock_complete_all_states_and_timings(self):
        expected_timeline = ["DETECTED", "VERIFYING", "PRODUCT_VERIFIED", "OPENING_BROWSER",
                             "CART_READY", "DRY_RUN_BOUNDARY_REACHED", "NOTIFYING", "COMPLETE"]
        for kind in ("NEW_PRODUCT", "RESTOCK"):
            with self.subTest(kind=kind):
                event = self.event(kind)
                result = await self.runner.process(event)
                self.assertEqual(result["status"], "COMPLETE")
                self.assertEqual(result["event_type"], kind)
                self.assertTrue(result["simulated"])
                self.assertFalse(result["boundary"]["order_created"])
                self.assertEqual(result["notification"]["status"], "SMTP_ACCEPTED")
                saved = self.store.get_event(event["id"])
                self.assertEqual(saved["details"]["timeline"], expected_timeline)
                self.assertEqual(saved["details"]["timing"], result["timing"])
                marks = result["timing"]["marks"]
                self.assertEqual(list(marks), [f"T{i}" for i in range(6)])
                monotonic = [marks[f"T{i}"]["monotonic_ns"] for i in range(6)]
                self.assertEqual(monotonic, sorted(monotonic))
                self.assertTrue(all(value >= 0 for value in result["timing"]["durations_ms"].values()))
                self.assertNotIn("T5", self.notifier.calls[-1]["timing"]["marks"])
                self.assertEqual(self.notifier.calls[-1]["boundary"]["status"], "DRY_RUN_BOUNDARY_REACHED")
                state_logs = [entry["state"] for entry in self.log.records if entry["kind"] == "STATE" and entry["event_id"] == event["id"]]
                self.assertEqual(state_logs, expected_timeline)
        self.assertEqual(self.store.summary()["notification_count"], 2)
        self.assertEqual(self.store.list_products(), [])
        self.assertFalse(self.store.summary()["baseline_initialized"])

    async def test_t5_exists_only_after_smtp_acceptance(self):
        for status in ("NOT_CONFIGURED", "NOTIFICATION_FAILED", "SENT"):
            with self.subTest(status=status):
                self.notifier.result = NotificationResult(status, error_code="FIXTURE_STATUS")
                result = await self.runner.process(self.event())
                self.assertEqual(result["status"], "COMPLETE")
                self.assertNotIn("T5", result["timing"]["marks"])
                self.assertIsNone(result["timing"]["durations_ms"]["detection_to_notification"])
                self.assertEqual(set(result["timing"]["marks"]), {f"T{i}" for i in range(5)})

    async def test_restart_and_fifty_repeated_scans_do_not_repeat_event_or_actions(self):
        self.store.ingest([replace(available_product(), availability="SOLD_OUT")])
        event = self.store.ingest([available_product()])["events"][0]
        await self.runner.process(event)
        before = deepcopy(self.provider.calls)
        self.reopen()
        self.assertEqual(self.store.recover_interrupted(), 0)
        for _ in range(50):
            self.assertEqual(self.store.ingest([available_product()])["events"], [])
        self.assertEqual((await self.runner.process(event))["status"], "ALREADY_CLAIMED")
        self.assertEqual(self.provider.calls, before)
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(self.store.summary()["event_count"], 1)

    async def test_notification_exception_is_isolated_and_does_not_retry_cart(self):
        self.notifier.error = RuntimeError("Fixture failure contains private-looking text and must not be logged")
        event = self.event()
        result = await self.runner.process(event)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["notification"]["error_code"], "NOTIFIER_ERROR")
        self.assertNotIn("T5", result["timing"]["marks"])
        self.assertEqual(self.store.summary()["notification_count"], 1)
        self.assertEqual((await self.runner.process(event))["status"], "ALREADY_CLAIMED")
        self.assertEqual(self.provider.calls.count(("prepare_cart", "87")), 1)
        self.assertNotIn("private-looking", str(self.log.records))

    async def test_stock_missing_or_unverified_never_opens_product_or_cart(self):
        scenarios = [
            (AutoGrabError("PRODUCT_MISSING"), available_product(), "PRODUCT_MISSING"),
            (None, replace(available_product(), availability="SOLD_OUT"), "OUT_OF_STOCK"),
            (None, replace(available_product(), availability="UNKNOWN"), "STOCK_UNKNOWN"),
            (None, None, "PRODUCT_UNVERIFIED"),
            (None, replace(available_product(), product_id="different"), "PRODUCT_MISMATCH"),
        ]
        for check_error, returned_product, code in scenarios:
            with self.subTest(code=code):
                self.provider.calls.clear()
                self.provider.check_error = check_error
                self.provider.product = returned_product
                event = self.event()
                result = await self.runner.process(event)
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["error_code"], code)
                self.assertEqual(self.provider.calls, [("check_product", "87")])
                self.assertEqual(self.notifier.calls, [])
                self.assertEqual(set(result["timing"]["marks"]), {"T0"})
                self.assertFalse(self.store.claim_event(event["id"]))

    async def test_unverified_boundary_never_notifies(self):
        valid_boundary = deepcopy(self.provider.boundary)
        candidates = [None, {}, {"status": "DRY_RUN_BOUNDARY_REACHED"},
                      {**valid_boundary, "status": "UNVERIFIED"},
                      {**valid_boundary, "order_created": True},
                      {**valid_boundary, "payment_url": "https://example.test/payment"}]
        for boundary in candidates:
            with self.subTest(boundary=boundary):
                self.provider.boundary = boundary
                result = await self.runner.process(self.event())
                self.assertEqual(result["status"], "FAILED")
                self.assertEqual(result["error_code"], "BOUNDARY_UNVERIFIED")
                self.assertEqual(set(result["timing"]["marks"]), {"T0", "T1", "T2", "T3"})
                self.assertEqual(self.notifier.calls, [])
        self.assertEqual(self.store.summary()["notification_count"], 0)

    async def test_duplicate_process_attempt_has_one_provider_workflow(self):
        event = self.event()
        results = await asyncio.gather(self.runner.process(event), self.runner.process(deepcopy(event)))
        self.assertEqual(sorted(result["status"] for result in results), ["ALREADY_CLAIMED", "COMPLETE"])
        self.assertEqual(self.provider.calls, [(name, "87") for name in (
            "check_product", "open_product", "prepare_cart", "dry_run_checkout")])
        self.assertEqual(len(self.notifier.calls), 1)

    async def test_cancellation_persists_interrupted_and_restart_never_retries_dispatch(self):
        self.provider.release_cart = asyncio.Event()
        event = self.event()
        task = asyncio.create_task(self.runner.process(event))
        await asyncio.wait_for(self.provider.cart_started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.get_event(event["id"])["status"], "INTERRUPTED")
        self.assertEqual(self.store.get_event(event["id"])["details"]["timeline"][-1], "OPENING_BROWSER")
        self.assertEqual(self.notifier.calls, [])
        before = deepcopy(self.provider.calls)
        self.reopen()
        self.assertEqual(self.store.recover_interrupted(), 0)
        self.assertEqual((await self.runner.process(event))["status"], "ALREADY_CLAIMED")
        self.assertEqual(self.provider.calls, before)

    async def test_captcha_login_and_uncertain_cart_are_terminal_without_retry(self):
        for code, terminal in (("CAPTCHA_REQUIRED", "CAPTCHA_REQUIRED"), ("LOGIN_REQUIRED", "LOGIN_REQUIRED"),
                               ("ACTION_OUTCOME_UNKNOWN", "FAILED")):
            with self.subTest(code=code):
                self.provider.cart_error = AutoGrabError(code)
                event = self.event()
                result = await self.runner.process(event)
                self.assertEqual(result["status"], terminal)
                self.assertEqual(result["error_code"], code)
                before = deepcopy(self.provider.calls)
                self.assertEqual((await self.runner.process(event))["status"], "ALREADY_CLAIMED")
                self.assertEqual(self.provider.calls, before)
                self.assertEqual(self.notifier.calls, [])

    async def test_caller_event_copy_cannot_override_persisted_product_identity(self):
        event = self.event()
        forged_copy = {**event, "product_id": "different", "provider": "different", "simulated": False}
        result = await self.runner.process(forged_copy)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(self.provider.calls[0], ("check_product", "87"))
        self.assertTrue(result["simulated"])
        self.assertEqual(self.notifier.calls[0]["event"]["product_id"], "87")


if __name__ == "__main__":
    unittest.main()
