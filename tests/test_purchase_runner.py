"""MOCK ONLY: unpaid-order coordinator uses no browser, SMTP or merchant API.

Preflight and receipt assertions below are synthetic contract evidence. Even
tests using origin REAL prove validation logic only, never a real unpaid order.
"""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from autograb.core.errors import AutoGrabError
from autograb.core.live import LiveGuard, Preflight, REQUIRED_CHECKS, SMTPProof, signal_disarm
from autograb.core.models import Product
from autograb.core.purchase import PurchaseRunner
from autograb.notifications.email import NotificationResult
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore


def product(product_id="87"):
    return Product(product_id=product_id, name="MOCK VPS", availability="AVAILABLE",
                   prices=[{"cents": 4999, "period": "Annually", "currency": "USD"}],
                   product_url="https://bandwagonhost.com/order/ecommerce", eligible=True)


def fixture_preflight(**overrides):
    checks = {name: "PASS" for name in REQUIRED_CHECKS}
    checks.update(overrides)
    return Preflight(checks, SMTPProof("SMTP_ACCEPTED", "REAL_SMTP", datetime.now(timezone.utc)))


def receipt(product_id="87", *, source="MOCK", order_id="123", invoice_id="456"):
    url = f"https://bandwagonhost.com/viewinvoice.php?id={invoice_id}"
    evidence = {
        "order_id": order_id, "invoice_id": invoice_id, "payment_url": url,
        "merchant": "bandwagonhost.com", "product_id": product_id, "invoice_status": "UNPAID",
        "merchant_verified": True, "product_verified": True, "amount_present": True,
        "payment_page_verified": True, "unpaid_verified": True, "source": source,
        "amount": "$49.99 USD", "billing": "Annually", "deadline": None,
    }
    return {"order_id": order_id, "invoice_id": invoice_id, "payment_url": url, "verification": evidence}


class MemoryLog:
    def __init__(self):
        self.records = []

    def write(self, kind, **fields):
        self.records.append({"kind": kind, **deepcopy(fields)})


class FakeProvider:
    """Pure in-memory fixture; order/payment endpoints do not exist."""
    simulation_only = True
    def __init__(self):
        self.calls = []
        self.products = {"87": product(), "88": product("88")}
        self.check_error = self.submit_error = self.reconcile_error = None
        self.submit_started = asyncio.Event()
        self.release_submit = None
        self.receipt_source = "MOCK"
        self.server_orders = {}
        self.commit_before_error = False
        self.reconciliation = None
        self.result_override = None
        self.checkout_verified = True
        self.inflight = 0
        self.max_inflight = 0

    async def check_product(self, product_id):
        self.calls.append(("check_product", product_id))
        if self.check_error:
            raise self.check_error
        return self.products[product_id]

    async def open_product(self, item):
        self.calls.append(("open_product", item.product_id))

    async def prepare_cart(self, item):
        self.calls.append(("prepare_cart", item.product_id))
        return {"configuration_verified": True}

    async def prepare_checkout(self, item, configuration):
        self.calls.append(("prepare_checkout", item.product_id))
        return {"cart_identifier": "0", "checkout_verified": self.checkout_verified,
                "private_hidden_form_value": "MUST_REMAIN_MEMORY_ONLY"}

    async def submit_unpaid_order(self, intent, item, checkout):
        self.calls.append(("submit_unpaid_order", item.product_id))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self.submit_started.set()
        try:
            if self.release_submit is not None:
                await self.release_submit.wait()
            await asyncio.sleep(0)
            found = receipt(item.product_id, source=self.receipt_source,
                            order_id=str(1000 + int(item.product_id)), invoice_id=str(2000 + int(item.product_id)))
            if self.submit_error:
                if self.commit_before_error:
                    self.server_orders[intent["intent_id"]] = deepcopy(found)
                raise self.submit_error
            self.server_orders[intent["intent_id"]] = deepcopy(found)
            return deepcopy(self.result_override) if self.result_override is not None else found
        finally:
            self.inflight -= 1

    async def reconcile_intent(self, intent, item):
        self.calls.append(("reconcile_intent", item.product_id))
        if self.reconcile_error:
            raise self.reconcile_error
        if self.reconciliation is not None:
            return deepcopy(self.reconciliation)
        if intent["intent_id"] in self.server_orders:
            return {"status": "FOUND", **deepcopy(self.server_orders[intent["intent_id"]])}
        return {"status": "ABSENT"}

    async def pay(self, *_):
        raise AssertionError("Payment is outside this interface")


class FakeNotifier:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.result = NotificationResult("SMTP_ACCEPTED")
        self.error = None
        self.notice_calls = []

    async def send_payment_ready(self, item, event, intent, timing):
        persisted = IntentStore(self.store).get(intent["intent_id"])
        assert persisted["state"] == "PAYMENT_READY"
        assert persisted["payment_page_verified"] is True
        self.calls.append(deepcopy(dict(product_id=item.product_id, event=event, intent=intent, timing=timing)))
        if self.error:
            raise self.error
        return self.result

    async def send_session_required(self, notice_id, status):
        self.notice_calls.append((notice_id, status))
        return NotificationResult("SMTP_ACCEPTED")


class PurchaseRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.path = self.root / "state.sqlite3"
        self.store = Store(self.path)
        self.provider = FakeProvider()
        self.notifier = FakeNotifier(self.store)
        self.log = MemoryLog()
        self.guard = LiveGuard(self.root, mode="LIVE")
        self.guard.arm(fixture_preflight(), duration=timedelta(hours=1))
        self.preflight_calls = 0
        self.preflight_checks = {}
        self.preflight_hook = None
        self.runner = self.make_runner()

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def preflight(self):
        self.preflight_calls += 1
        if self.preflight_hook:
            self.preflight_hook(self.preflight_calls)
        return fixture_preflight(**self.preflight_checks)

    def make_runner(self, *, allow_simulated=True):
        return PurchaseRunner(self.store, self.provider, self.notifier, self.log,
                              self.guard, self.preflight, allow_simulated=allow_simulated)

    def event(self, item=None, *, simulated=True, kind="RESTOCK"):
        return self.store.create_event(item or product(), kind, simulated=simulated)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.notifier.store = self.store
        self.runner = self.make_runner()

    def submit_count(self):
        return sum(name == "submit_unpaid_order" for name, _ in self.provider.calls)

    async def test_default_runner_refuses_simulated_events_without_browser_or_order(self):
        self.runner = self.make_runner(allow_simulated=False)
        result = await self.runner.process(self.event())
        self.assertEqual(result["error_code"], "SIMULATED_EVENT_CANNOT_ORDER")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.runner.intents.list(), [])
        self.assertEqual(result["origin"], "SIMULATED")

    async def test_real_provider_cannot_enable_simulated_purchases(self):
        self.provider.simulation_only = False
        with self.assertRaises(ValueError):
            self.make_runner(allow_simulated=True)
        self.runner = self.make_runner(allow_simulated=False)
        result = await self.runner.process(self.event())
        self.assertEqual(result["error_code"], "SIMULATED_EVENT_CANNOT_ORDER")
        self.assertEqual(self.provider.calls, [])

    async def test_simulation_exception_is_revoked_if_provider_changes_after_construction(self):
        self.provider.simulation_only = False
        result = await self.runner.process(self.event())
        self.assertEqual(result["error_code"], "SIMULATED_EVENT_CANNOT_ORDER")
        self.assertEqual(self.provider.calls, [])

    async def test_preexisting_pending_event_never_enters_new_live_worker(self):
        event = self.event(simulated=False)
        self.runner = self.make_runner(allow_simulated=False)
        result = await self.runner.process(event)
        self.assertEqual(result["status"], "STALE_EVENT")
        self.assertEqual(result["error_code"], "EVENT_PRECEDES_WORKER")
        self.assertEqual(self.store.get_event(event["id"])["status"], "PENDING")
        self.assertEqual(self.runner.intents.list(), [])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.preflight_calls, 0)

    async def test_row_watermark_accepts_later_event_even_when_timestamps_match(self):
        old = self.event(item=product("88"))
        self.runner = self.make_runner()
        fresh = self.event()
        self.store.connection.execute("UPDATE events SET created_at=? WHERE id=?", (old["created_at"], fresh["id"]))
        self.assertEqual((await self.runner.process(old))["status"], "STALE_EVENT")
        result = await self.runner.process(fresh)
        self.assertEqual(result["status"], "WAITING_FOR_USER")
        self.assertEqual(self.submit_count(), 1)

    async def test_fresh_event_concurrent_calls_submit_once(self):
        event = self.event()
        results = await asyncio.gather(*(self.runner.process(event) for _ in range(8)))
        self.assertEqual(sum(result["status"] == "WAITING_FOR_USER" for result in results), 1)
        self.assertEqual(sum(result["status"] == "ORDER_ALREADY_EXISTS" for result in results), 7)
        self.assertEqual(self.submit_count(), 1)
        self.assertEqual(len(self.notifier.calls), 1)

    async def test_two_workers_cannot_claim_same_fresh_pending_event(self):
        second_worker = self.make_runner()
        event = self.event()
        results = await asyncio.gather(self.runner.process(event), second_worker.process(event))
        self.assertEqual(sum(result["status"] == "WAITING_FOR_USER" for result in results), 1)
        self.assertEqual(self.submit_count(), 1)
        self.assertEqual(len(self.runner.intents.list()), 1)

    async def test_provider_simulation_flag_lost_before_submit_blocks_dispatch_and_recovery(self):
        def replace_adapter_mode(count):
            if count == 3:
                self.provider.simulation_only = False
        self.preflight_hook = replace_adapter_mode
        result = await self.runner.process(self.event())
        self.assertEqual(result["error_code"], "SIMULATED_EVENT_CANNOT_ORDER")
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.submit_count(), 0)
        self.assertFalse(any(name == "reconcile_intent" for name, _ in self.provider.calls))

    async def test_simulated_recovery_cannot_use_a_real_provider(self):
        event = self.event()
        intent = self.runner.intents.create(event, product())
        self.runner.intents.set_cart(intent["intent_id"], "0")
        self.runner.intents.mark_checkout_ready(intent["intent_id"])
        self.runner.intents.mark_submitting(intent["intent_id"])
        self.provider.server_orders[intent["intent_id"]] = receipt()
        self.provider.simulation_only = False
        results = await self.runner.recover()
        self.assertEqual(results, [])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.notifier.calls, [])
        self.assertEqual(self.runner.intents.get(intent["intent_id"])["state"], "RECONCILIATION_REQUIRED")

    async def test_mock_opportunity_reaches_unpaid_verified_receipt_then_notifies_once(self):
        event = self.event()
        result = await self.runner.process(event)
        self.assertEqual(result["status"], "WAITING_FOR_USER")
        self.assertEqual(result["evidence_source"], "MOCK")
        self.assertEqual(result["origin"], "SIMULATED")
        self.assertEqual(self.submit_count(), 1)
        self.assertEqual(self.preflight_calls, 3)
        self.assertEqual(result["notification"]["status"], "SMTP_ACCEPTED")
        self.assertEqual(set(result["timing"]["marks"]), {f"T{i}" for i in range(10)})
        self.assertEqual(self.notifier.calls[0]["intent"]["state"], "PAYMENT_READY")
        self.assertNotIn("T9", self.notifier.calls[0]["timing"]["marks"])
        stored = self.store.get_event(event["id"])
        self.assertEqual(stored["details"]["purchase_timeline"][-2:], ["PAYMENT_READY", "WAITING_FOR_USER"])
        self.assertEqual(self.runner.intents.get(result["intent_id"])["state"], "WAITING_FOR_USER")

    async def test_live_guard_dry_run_disarmed_and_bad_email_all_stop_before_cart(self):
        for case in ("DRY_RUN", "DISARMED", "EMAIL_BAD"):
            with self.subTest(case=case):
                self.guard = LiveGuard(self.root, mode="DRY_RUN" if case == "DRY_RUN" else "LIVE")
                if case == "EMAIL_BAD":
                    self.guard.arm(fixture_preflight())
                    self.preflight_checks = {"email": "NOT_READY"}
                self.runner = self.make_runner()
                result = await self.runner.process(self.event())
                self.assertEqual(result["status"], "LIVE_NOT_ARMED")
                self.assertEqual(self.provider.calls, [])
                self.assertEqual(self.submit_count(), 0)
                self.assertEqual(self.runner.intents.list(), [])

    async def test_known_baseline_remains_no_op_when_live_arms(self):
        first = self.store.ingest([product(), product("88")])
        self.assertEqual(first["events"], [])
        self.guard.disarm()
        self.guard.arm(fixture_preflight())
        for _ in range(10):
            current = self.store.ingest([product(), product("88")])
            for event in current["events"]:
                await self.runner.process(event)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.runner.intents.list(), [])

    async def test_price_does_not_reject_eligible_product(self):
        item = replace(product(), prices=[{"cents": 99999999, "period": "Annually", "currency": "USD"}])
        self.provider.products["87"] = item
        result = await self.runner.process(self.event(item))
        self.assertEqual(result["status"], "WAITING_FOR_USER")
        self.assertEqual(self.submit_count(), 1)

    async def test_unqualified_event_or_product_never_reaches_cart(self):
        cases = [dict(kind="PRODUCT_CHANGED"), dict(item=replace(product(), eligible=False)),
                 dict(item=replace(product(), availability="SOLD_OUT"))]
        for options in cases:
            with self.subTest(options=options):
                result = await self.runner.process(self.event(**options))
                self.assertEqual(result["error_code"], "EVENT_NOT_LIVE_ELIGIBLE")
        self.assertEqual(self.provider.calls, [])

    async def test_fresh_stock_and_product_identity_are_verified_before_cart(self):
        for item, expected in ((replace(product(), availability="SOLD_OUT"), "OUT_OF_STOCK"),
                               (replace(product(), availability="UNKNOWN"), "STOCK_UNKNOWN"),
                               (replace(product(), eligible=False), "PRODUCT_NOT_PROMOTIONAL"),
                               (product("88"), "PRODUCT_MISMATCH")):
            with self.subTest(expected=expected):
                self.provider.products["87"] = item
                result = await self.runner.process(self.event())
                self.assertEqual(result["error_code"], expected)
                self.assertEqual(self.submit_count(), 0)
                self.assertEqual(self.runner.intents.get(result["intent_id"])["state"], "PRE_SUBMIT_ABORTED")
        self.assertTrue(all(name == "check_product" for name, _ in self.provider.calls))

    async def test_kill_switch_after_durable_marker_stops_post_and_reconciles(self):
        self.preflight_hook = lambda count: signal_disarm(self.root) if count == 3 else None
        result = await self.runner.process(self.event())
        self.assertEqual(result["status"], "ORDER_SUBMIT_FAILED")
        self.assertEqual(result["error_code"], "ORDER_ABSENCE_CONFIRMED_NO_AUTORETRY")
        self.assertEqual(self.submit_count(), 0)
        self.assertEqual(self.provider.calls[-1][0], "reconcile_intent")
        intent = self.runner.intents.get(result["intent_id"])
        self.assertIsNotNone(intent["submit_started_at"])
        self.assertEqual(intent["reconciliation_status"], "ABSENT")
        self.assertFalse(self.runner.intents.mark_submitting(intent["intent_id"]))

    async def test_email_unavailable_on_last_preflight_stops_dispatch(self):
        def disable_email(count):
            if count == 3:
                self.preflight_checks = {"email": "NOT_READY"}
        self.preflight_hook = disable_email
        result = await self.runner.process(self.event())
        self.assertEqual(self.submit_count(), 0)
        self.assertEqual(result["status"], "ORDER_SUBMIT_FAILED")

    async def test_timeout_before_server_commit_proves_absence_without_retry(self):
        self.provider.submit_error = TimeoutError("private provider body must not be logged")
        result = await self.runner.process(self.event())
        self.assertEqual(result["status"], "ORDER_SUBMIT_FAILED")
        self.assertEqual(self.submit_count(), 1)
        self.assertEqual(len(self.provider.server_orders), 0)
        self.assertEqual(self.notifier.calls, [])
        self.assertNotIn("private provider body", repr(self.log.records))
        self.reopen()
        await self.runner.recover()
        self.assertEqual(self.submit_count(), 1)

    async def test_timeout_after_server_commit_finds_existing_order_no_duplicate(self):
        self.provider.submit_error = TimeoutError()
        self.provider.commit_before_error = True
        result = await self.runner.process(self.event())
        self.assertEqual(result["status"], "WAITING_FOR_USER")
        self.assertEqual(self.submit_count(), 1)
        self.assertEqual(len(self.provider.server_orders), 1)
        self.assertEqual(self.runner.intents.get(result["intent_id"])["reconciliation_status"], "FOUND")

    async def test_unknown_timeout_result_remains_locked_and_cannot_create_replacement(self):
        self.provider.submit_error = ConnectionResetError()
        self.provider.reconciliation = {"status": "UNKNOWN"}
        first = await self.runner.process(self.event())
        self.assertEqual(first["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.submit_count(), 1)
        self.reopen()
        second = await self.runner.process(self.event())
        self.assertEqual(second["status"], "ORDER_ALREADY_EXISTS")
        self.assertEqual(second["intent_id"], first["intent_id"])
        self.assertEqual(self.submit_count(), 1)

    async def test_repeated_event_and_new_event_same_active_product_never_resubmit(self):
        event = self.event()
        first = await self.runner.process(event)
        again = await self.runner.process(event)
        another = await self.runner.process(self.event())
        self.assertEqual(again["status"], "ORDER_ALREADY_EXISTS")
        self.assertEqual(another["status"], "ORDER_ALREADY_EXISTS")
        self.assertEqual(first["intent_id"], another["intent_id"])
        self.assertEqual(self.submit_count(), 1)
        self.assertEqual(len(self.notifier.calls), 1)

    async def test_real_opportunity_conflicting_with_simulated_intent_keeps_mock_evidence_label(self):
        first = await self.runner.process(self.event())
        result = await self.runner.process(self.event(simulated=False))
        self.assertEqual(result["status"], "SIMULATED_INTENT_CONFLICT")
        self.assertEqual(result["request_origin"], "REAL")
        self.assertEqual(result["origin"], "SIMULATED")
        self.assertEqual(result["evidence_source"], "MOCK")
        self.assertEqual(result["intent_id"], first["intent_id"])
        self.assertEqual(self.submit_count(), 1)
        self.assertEqual(len(self.notifier.calls), 1)

    async def test_blocked_real_opportunity_reports_no_real_site_evidence(self):
        self.guard.disarm()
        result = await self.runner.process(self.event(simulated=False))
        self.assertEqual(result["origin"], "REAL")
        self.assertEqual(result["evidence_source"], "NOT_VERIFIED")
        self.assertEqual(self.provider.calls, [])

    async def test_concurrent_origin_conflict_is_not_reported_as_real_existing_order(self):
        def create_competing_fixture(count):
            if count == 1:
                self.runner.intents.create(self.event(), product())
        self.preflight_hook = create_competing_fixture
        event = self.event(simulated=False)
        result = await self.runner.process(event)
        self.assertEqual(result["status"], "SIMULATED_INTENT_CONFLICT")
        self.assertEqual(result["origin"], "SIMULATED")
        self.assertEqual(result["request_origin"], "REAL")
        self.assertEqual(result["evidence_source"], "NOT_VERIFIED")
        self.assertEqual(self.store.get_event(event["id"])["status"], "SIMULATED_INTENT_CONFLICT")
        self.assertEqual(self.submit_count(), 0)

    async def test_single_worker_serializes_different_products(self):
        results = await asyncio.gather(self.runner.process(self.event()), self.runner.process(self.event(product("88"))))
        self.assertTrue(all(result["status"] == "WAITING_FOR_USER" for result in results))
        self.assertEqual(self.submit_count(), 2)
        self.assertEqual(self.provider.max_inflight, 1)

    async def test_cancellation_during_post_retains_durable_uncertainty(self):
        self.provider.release_submit = asyncio.Event()
        event = self.event()
        task = asyncio.create_task(self.runner.process(event))
        await asyncio.wait_for(self.provider.submit_started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        intent = self.runner.intents.get_by_event(event["id"])
        self.assertEqual(intent["state"], "RECONCILIATION_REQUIRED")
        self.assertFalse(self.runner.intents.mark_submitting(intent["intent_id"]))
        self.assertEqual(self.submit_count(), 1)

    async def test_recovery_is_read_only_and_works_disarmed_after_restart(self):
        event = self.event()
        intent = self.runner.intents.create(event, product())
        self.runner.intents.set_cart(intent["intent_id"], "0")
        self.runner.intents.mark_checkout_ready(intent["intent_id"])
        self.runner.intents.mark_submitting(intent["intent_id"])
        self.provider.server_orders[intent["intent_id"]] = receipt()
        self.reopen()
        self.guard.disarm()
        result = (await self.runner.recover())[0]
        self.assertEqual(result["status"], "WAITING_FOR_USER")
        self.assertTrue(result["recovery"])
        self.assertEqual(self.provider.calls, [("reconcile_intent", "87")])
        self.assertEqual(self.submit_count(), 0)
        self.assertEqual(self.preflight_calls, 0)
        self.assertNotIn("T0", result["timing"]["marks"])
        self.assertNotIn("T6", result["timing"]["marks"])
        self.assertIsNone(result["timing"]["durations_ms"]["detection_to_payment_ready"])

    async def test_crash_between_payment_persistence_and_email_is_recoverable(self):
        event = self.event()
        intent = self.runner.intents.create(event, product())
        self.runner.intents.set_cart(intent["intent_id"], "0")
        self.runner.intents.mark_checkout_ready(intent["intent_id"])
        self.runner.intents.mark_submitting(intent["intent_id"])
        found = receipt()
        self.runner.intents.persist_result(intent["intent_id"], **found)
        self.provider.server_orders[intent["intent_id"]] = found
        self.reopen()
        result = (await self.runner.recover())[0]
        self.assertEqual(result["status"], "WAITING_FOR_USER")
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(self.submit_count(), 0)

    async def test_inconclusive_recovery_never_reannounces_historical_payment_ready(self):
        event = self.event()
        intent = self.runner.intents.create(event, product())
        self.runner.intents.set_cart(intent["intent_id"], "0")
        self.runner.intents.mark_checkout_ready(intent["intent_id"])
        self.runner.intents.mark_submitting(intent["intent_id"])
        self.runner.intents.persist_result(intent["intent_id"], **receipt())
        for response in ({"status": "UNKNOWN"}, {"status": "FOUND", **receipt(), "verification": {}}):
            with self.subTest(response=response):
                self.provider.reconciliation = response
                result = (await self.runner.recover())[0]
                self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
                self.assertNotIn("T8", result["timing"]["marks"])
                self.assertEqual(self.notifier.calls, [])
                self.assertEqual(self.submit_count(), 0)
                self.assertEqual(self.runner.intents.get(intent["intent_id"])["order_id"], "123")

    async def test_notification_failure_keeps_payment_ready_identifiers_and_no_email_timing(self):
        self.notifier.error = RuntimeError("fixture SMTP secret must not be logged")
        result = await self.runner.process(self.event())
        self.assertEqual(result["status"], "WAITING_FOR_USER")
        self.assertEqual(result["notification"]["status"], "NOTIFICATION_FAILED")
        self.assertNotIn("T9", result["timing"]["marks"])
        intent = self.runner.intents.get(result["intent_id"])
        self.assertIsNotNone(intent["order_id"])
        self.assertIsNotNone(intent["invoice_id"])
        self.assertTrue(intent["payment_page_verified"])
        self.assertEqual(self.submit_count(), 1)
        self.assertNotIn("fixture SMTP secret", repr(self.log.records))

    async def test_partial_order_receipt_stops_without_payment_ready_or_email(self):
        self.provider.result_override = {"order_id": "123"}
        result = await self.runner.process(self.event())
        self.assertEqual(result["status"], "INVOICE_NOT_FOUND")
        self.assertEqual(self.runner.intents.get(result["intent_id"])["state"], "ORDER_CREATED")
        self.assertEqual(self.notifier.calls, [])
        self.assertNotIn("T8", result["timing"]["marks"])

    async def test_invoice_url_without_verified_page_never_becomes_payment_ready(self):
        self.provider.result_override = {key: value for key, value in receipt().items() if key != "verification"}
        result = await self.runner.process(self.event())
        self.assertEqual(result["status"], "PAYMENT_URL_READY")
        self.assertEqual(result["error_code"], "PAYMENT_PAGE_UNVERIFIED")
        self.assertEqual(self.notifier.calls, [])

    async def test_login_or_captcha_retains_human_status_and_sends_status_only_notice(self):
        for status in ("LOGIN_REQUIRED", "SESSION_EXPIRED", "CAPTCHA_REQUIRED"):
            with self.subTest(status=status):
                self.provider.check_error = AutoGrabError(status)
                result = await self.runner.process(self.event())
                self.assertEqual(result["status"], status)
                self.assertEqual(self.notifier.notice_calls[-1][1], status)
                self.assertEqual(self.submit_count(), 0)

    async def test_preflight_session_blocks_disarm_and_send_one_human_notice(self):
        for status in ("LOGIN_REQUIRED", "SESSION_EXPIRED", "CAPTCHA_REQUIRED"):
            with self.subTest(status=status):
                self.guard.arm(fixture_preflight())
                self.preflight_checks = {"session": status}
                prior_notices = len(self.notifier.notice_calls)
                result = await self.runner.process(self.event())
                self.assertEqual(result["status"], status)
                self.assertEqual(result["error_code"], status)
                self.assertEqual(len(self.notifier.notice_calls), prior_notices + 1)
                self.assertEqual(self.notifier.notice_calls[-1][1], status)
                self.assertFalse(self.guard.status()["armed"])
                self.assertEqual(self.provider.calls, [])
                self.assertEqual(self.submit_count(), 0)

    async def test_session_expiry_at_last_guard_notifies_once_even_if_reconciliation_also_needs_login(self):
        def expire_session(count):
            if count == 3:
                self.preflight_checks = {"session": "SESSION_EXPIRED"}
        self.preflight_hook = expire_session
        self.provider.reconcile_error = AutoGrabError("LOGIN_REQUIRED")
        result = await self.runner.process(self.event())
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.submit_count(), 0)
        self.assertEqual(len(self.notifier.notice_calls), 1)
        self.assertEqual(self.notifier.notice_calls[0][1], "SESSION_EXPIRED")
        self.assertFalse(self.guard.status()["armed"])

    async def test_recovery_login_failure_preserves_intent_and_never_dispatches(self):
        event = self.event()
        intent = self.runner.intents.create(event, product())
        self.runner.intents.set_cart(intent["intent_id"], "0")
        self.runner.intents.mark_checkout_ready(intent["intent_id"])
        self.runner.intents.mark_submitting(intent["intent_id"])
        self.provider.reconcile_error = AutoGrabError("SESSION_EXPIRED")
        result = (await self.runner.recover())[0]
        self.assertEqual(result["error_code"], "SESSION_EXPIRED")
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.notifier.notice_calls[-1][1], "SESSION_EXPIRED")
        self.assertEqual(self.submit_count(), 0)

    async def test_actual_origin_cannot_accept_mock_receipt_as_real_verification(self):
        self.runner = self.make_runner(allow_simulated=False)
        event = self.event(simulated=False)
        result = await self.runner.process(event)
        self.assertNotEqual(result["status"], "WAITING_FOR_USER")
        self.assertFalse(self.runner.intents.get(result["intent_id"])["payment_page_verified"])
        self.assertEqual(self.notifier.calls, [])
        self.assertEqual(self.submit_count(), 1)

    async def test_private_checkout_fields_and_provider_exception_never_persist(self):
        event = self.event()
        result = await self.runner.process(event)
        everything = repr(self.log.records) + repr(self.store.get_event(event["id"])) + repr(self.runner.intents.get(result["intent_id"]))
        self.assertNotIn("MUST_REMAIN_MEMORY_ONLY", everything)
        self.assertNotIn("private_hidden_form_value", everything)
        self.assertNotIn("pay", [name for name, _ in self.provider.calls])


if __name__ == "__main__":
    unittest.main()
