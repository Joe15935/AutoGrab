"""Financial-boundary regressions using temporary SQLite and offline fixtures.

No merchant, browser, SMTP server or real order is used. Synthetic REAL_SMTP
preflight labels below exercise the gate contract, not real acceptance evidence.
"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from autograb.core.errors import AutoGrabError
from autograb.core.live import (Preflight, RealOrderSmokeGuard, REQUIRED_CHECKS, SMTPProof,
                                clear_signal, signal_disarm, signal_stop_monitoring)
from autograb.core.models import Product
from autograb.core.purchase import PurchaseRunner
from autograb.notifications.email import NotificationResult
from autograb.storage.database import Store
from autograb.storage.intents import IntentConflict, IntentStore


def product(provider):
    return Product("87", "Offline smoke fixture", "AVAILABLE",
        [{"cents":7990,"currency":"USD","period":"monthly","available":True}],
        "https://bandwagonhost.com/order/ecommerce" if provider == "bandwagon" else "https://www.dmit.io/cart.php",
        provider=provider, eligible=True)


def precheck(**changes):
    return {"tab_id":1,"stage":"CHECKOUT","code":"ORDER_PRECHECK_READY","login":"VALID","challenge":"NONE",
        "outcome":"PRECHECK_READY","precheck_id":str(uuid4()),"amount_cents":7990,"currency":"USD",
        "billing":"monthly","product_verified":True,"no_charge_verified":True,
        "observed_at":datetime.now(timezone.utc).isoformat(),**changes}


def receipt(provider):
    merchant = {"bandwagon":"bandwagonhost.com","dmit":"www.dmit.io"}[provider]
    return {"provider":provider,"merchant":merchant,"product_id":"87","source":"MOCK",
        "order_id":"123","invoice_id":"456","payment_url":f"https://{merchant}/viewinvoice.php?id=456",
        "invoice_status":"UNPAID","amount":"USD 79.90","amount_cents":7990,"currency":"USD","billing":"monthly",
        "merchant_verified":True,"product_verified":True,"amount_present":True,"payment_page_verified":True,"unpaid_verified":True}


def absence(intent):
    return {"tab_id":1,"stage":"ACCOUNT","code":"ORDER_ABSENCE_VERIFIED","login":"VALID","challenge":"NONE",
        "outcome":"NO_ORDER_FOUND","amount_cents":7990,"currency":"USD","billing":"monthly",
        "product_verified":True,"observed_at":datetime.now(timezone.utc).isoformat(),
        "submission_nonce":intent["submission_nonce"],"scope_complete":True,"time_window_verified":True}


class OrderSmokeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.intents = IntentStore(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ready(self, provider):
        item = product(provider)
        event = self.store.create_event(item, "RESTOCK", simulated=True)
        intent = self.intents.create(event, item)
        self.intents.set_cart(intent["intent_id"], "configuration_0")
        self.intents.mark_checkout_ready(intent["intent_id"])
        return intent["intent_id"], event, item

    def compete(self, action):
        barrier = threading.Barrier(2)
        def worker():
            with Store(self.path) as connection:
                ledger = IntentStore(connection)
                barrier.wait(timeout=10)
                return action(ledger)
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(lambda _:worker(), range(2)))

    def test_two_connections_claim_one_nonce_and_crash_absence_never_reopens_submit(self):
        for provider in ("bandwagon", "dmit"):
            with self.subTest(provider=provider):
                identity, event, item = self.ready(provider)
                check = precheck()
                self.intents.set_order_precheck(identity, check)
                def begin(ledger):
                    nonce = str(uuid4())
                    return nonce, ledger.begin_order_submission(identity, nonce, check["precheck_id"])
                results = self.compete(begin)
                self.assertEqual(sum(success for _,success in results), 1)
                saved = self.intents.get(identity)
                self.assertEqual(saved["state"], "ORDER_SUBMITTING")
                self.assertEqual(saved["submission_nonce"], next(nonce for nonce,won in results if won))
                self.assertIsNotNone(saved["submit_started_at"])
                # A new connection models recovery after dispatch was persisted.
                with Store(self.path) as reopened:
                    ledger = IntentStore(reopened)
                    recovered = ledger.recover_interrupted()
                    self.assertEqual([r["intent_id"] for r in recovered], [identity])
                    self.assertEqual(ledger.get(identity)["state"], "ORDER_UNCERTAIN")
                    self.assertFalse(ledger.begin_order_submission(identity, str(uuid4()), check["precheck_id"]))
                    with self.assertRaises((ValueError, AutoGrabError)):
                        ledger.reconcile(identity, "ABSENT")
                    self.assertEqual(ledger.get(identity)["state"], "ORDER_UNCERTAIN")
                    ledger.reconcile(identity, "ABSENT", absence_evidence=absence(saved))
                    self.assertFalse(ledger.begin_order_submission(identity, str(uuid4()), check["precheck_id"]))
                    with self.assertRaises(ValueError):
                        ledger.set_order_precheck(identity, precheck())
                    with self.assertRaises(IntentConflict):
                        ledger.create(event, item)
                    self.assertEqual(ledger.get(identity)["submission_nonce"], saved["submission_nonce"])

    def test_precheck_quote_session_no_charge_and_freshness_rejections_do_not_write(self):
        identity, _, _ = self.ready("bandwagon")
        for changes in ({"amount_cents":7991}, {"currency":"EUR"}, {"billing":"annually"},
                        {"login":"REQUIRED"}, {"challenge":"REQUIRED"}, {"no_charge_verified":False},
                        {"product_verified":False}, {"observed_at":(datetime.now(timezone.utc)-timedelta(seconds=61)).isoformat()},
                        {"observed_at":(datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, AutoGrabError)):
                self.intents.set_order_precheck(identity, precheck(**changes))
            self.assertEqual(self.intents.get(identity)["state"], "CHECKOUT_READY")
            self.assertIsNone(self.intents.get(identity)["submission_nonce"])
        check = precheck()
        self.intents.set_order_precheck(identity, check)
        self.assertFalse(self.intents.begin_order_submission(identity, str(uuid4()), str(uuid4())))
        self.assertIsNone(self.intents.get(identity)["submit_started_at"])

    def test_recovered_payment_identity_and_notification_claim_are_provider_bound(self):
        for provider in ("bandwagon", "dmit"):
            with self.subTest(provider=provider):
                identity, _, _ = self.ready(provider)
                check = precheck(); self.intents.set_order_precheck(identity, check)
                self.assertTrue(self.intents.begin_order_submission(identity, str(uuid4()), check["precheck_id"]))
                self.intents.mark_uncertain(identity)
                self.assertFalse(self.intents.claim_payment_notification(identity))
                evidence = receipt(provider)
                for changes in ({"provider":"dmit" if provider=="bandwagon" else "bandwagon"},
                                {"product_id":"88"}, {"amount_cents":1}, {"currency":"EUR"},
                                {"source":"REAL_SITE"}, {"invoice_status":"PAID"}):
                    bad = {**evidence,**changes}
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        self.intents.reconcile(identity,"FOUND",order_id="123",invoice_id="456",
                            payment_url=evidence["payment_url"],verification=bad)
                    self.assertEqual(self.intents.get(identity)["state"], "ORDER_UNCERTAIN")
                saved = self.intents.reconcile(identity,"FOUND",order_id="123",invoice_id="456",
                    payment_url=evidence["payment_url"],verification=evidence)
                self.assertEqual((saved["state"],saved["provider"],saved["origin"]), ("PAYMENT_READY",provider,"SIMULATED"))
                self.assertIsNotNone(saved["order_created_at"])
                self.assertIsNotNone(saved["payment_ready_at"])
                self.assertEqual(sum(self.compete(lambda ledger:ledger.claim_payment_notification(identity))), 1)
                with Store(self.path) as reopened:
                    self.assertFalse(IntentStore(reopened).claim_payment_notification(identity))


class OrderSmokeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.now, self.ns = datetime.now(timezone.utc), 1_000_000_000
        self.identity, self.precheck_id = str(uuid4()), str(uuid4())

    def guard(self, provider="bandwagon", mode="LIVE"):
        return RealOrderSmokeGuard(self.data,provider=provider,mode=mode,utc_clock=lambda:self.now,clock=lambda:self.ns)

    def preflight(self):
        return Preflight({key:"PASS" for key in REQUIRED_CHECKS},SMTPProof("SMTP_ACCEPTED","REAL_SMTP",self.now),self.now)

    def test_smoke_is_separate_one_use_and_cannot_be_revived_by_ordinary_arm(self):
        for provider in ("bandwagon", "dmit"):
            with self.subTest(provider=provider):
                guard = self.guard(provider)
                self.assertFalse(guard.status()["REAL_ORDER_SMOKE_TEST_ARMED"])
                guard.arm(self.preflight())
                with self.assertRaises(AutoGrabError): guard.issue_permit(self.identity,self.precheck_id,self.preflight())
                with self.assertRaises(AutoGrabError): guard.arm_smoke(self.preflight(),self.identity,confirmed=False)
                guard.arm_smoke(self.preflight(),self.identity,confirmed=True)
                permit = guard.issue_permit(self.identity,self.precheck_id,self.preflight())
                guard.assert_permit(self.identity,permit,self.preflight())
                with self.assertRaises(AutoGrabError): guard.issue_permit(self.identity,self.precheck_id,self.preflight())
                self.now += timedelta(seconds=61); self.ns += 61_000_000_000
                with self.assertRaises(AutoGrabError): guard.assert_permit(self.identity,permit,self.preflight())
                guard.arm(self.preflight())
                with self.assertRaises(AutoGrabError): guard.assert_permit(self.identity,permit,self.preflight())
                self.assertFalse(guard.status()["REAL_ORDER_SMOKE_TEST_ARMED"])

    def test_expiry_controls_and_forged_scope_refuse_permit(self):
        guard = self.guard()
        with self.assertRaises(AutoGrabError): guard.arm_smoke(self.preflight(),self.identity,confirmed=True,duration=timedelta(seconds=61))
        with self.assertRaises(AutoGrabError): self.guard(mode="DRY_RUN").arm_smoke(self.preflight(),self.identity,confirmed=True)
        guard.arm_smoke(self.preflight(),self.identity,confirmed=True)
        permit = guard.issue_permit(self.identity,self.precheck_id,self.preflight())
        for changes in ({"nonce":str(uuid4())},{"precheck_id":str(uuid4())},{"real_order_smoke_test_armed":False},
                        {"expires_at":(self.now+timedelta(hours=1)).isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(AutoGrabError):
                guard.assert_permit(self.identity,{**permit,**changes},self.preflight())
        with self.assertRaises(AutoGrabError): guard.assert_permit(str(uuid4()),permit,self.preflight())
        signal_stop_monitoring(self.data)
        with self.assertRaises(AutoGrabError): guard.assert_permit(self.identity,permit,self.preflight())
        clear_signal(self.data,"stop_monitoring",explicit=True)
        guard.arm_smoke(self.preflight(),self.identity,confirmed=True)
        permit = guard.issue_permit(self.identity,self.precheck_id,self.preflight())
        signal_disarm(self.data)
        with self.assertRaises(AutoGrabError): guard.assert_permit(self.identity,permit,self.preflight())


class OfflineOrderProvider:
    """Records fixture receipts only; deliberately has no browser or HTTP API."""
    simulation_only = True

    def __init__(self, path, provider):
        self.path, self.provider_name = path, provider
        self.prechecks = self.submits = self.lookups = 0
        self.orders = {}
        self.cancel_after_commit = False
        self.lookup_changes = {}
        self.check_changes = {}

    async def precheck_order(self, intent, item):
        assert intent["provider"] == item.provider == self.provider_name
        self.prechecks += 1
        return precheck(**self.check_changes)

    async def submit_order(self, intent, item, permit):
        # Prove a different SQLite reader sees the nonce BEFORE dispatch.
        with Store(self.path) as connection:
            persisted = IntentStore(connection).get(intent["intent_id"])
            assert persisted["state"] == "ORDER_SUBMITTING"
            assert persisted["submission_nonce"] == permit["nonce"]
            assert persisted["order_precheck"]["precheck_id"] == permit["precheck_id"]
        assert intent["provider"] == item.provider == self.provider_name
        self.submits += 1
        evidence = receipt(self.provider_name)
        self.orders[intent["intent_id"]] = {"status":"FOUND",
            **{key:evidence[key] for key in ("order_id","invoice_id","payment_url")},"verification":evidence}
        if self.cancel_after_commit:
            raise asyncio.CancelledError()
        return deepcopy(self.orders[intent["intent_id"]])

    async def reconcile_intent(self, intent, item):
        assert intent["provider"] == item.provider == self.provider_name
        self.lookups += 1
        if intent["intent_id"] not in self.orders:
            return {"status":"NO_ORDER_FOUND","absence_evidence":absence(intent)}
        found = deepcopy(self.orders[intent["intent_id"]])
        found["verification"].update(self.lookup_changes)
        return found


class OfflineOrderNotifier:
    def __init__(self, path):
        self.path, self.calls = path, []

    async def send_payment_ready(self, item, event, intent, timing):
        with Store(self.path) as connection:
            persisted = IntentStore(connection).get(intent["intent_id"])
            assert persisted["state"] == "PAYMENT_READY"
            assert persisted["payment_page_verified"] is True
            assert persisted["notification_claimed_at"] is not None
            assert persisted["provider"] == item.provider == event["provider"]
            assert persisted["origin"] == "SIMULATED" and event["simulated"]
        self.calls.append(intent["intent_id"])
        # A notification failure must not permit either a second submit or send.
        return NotificationResult("NOTIFICATION_FAILED", "OFFLINE_FIXTURE")


class OfflineLog:
    def write(self, *_args, **_fields):
        pass


class OrderSmokeRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.intents = IntentStore(self.store)
        self.notifier = OfflineOrderNotifier(self.path)
        self.preflight_calls = 0
        self.stop_after_persist = False

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def preflight(self):
        self.preflight_calls += 1
        if self.stop_after_persist and self.preflight_calls == 2:
            signal_stop_monitoring(Path(self.temp.name) / "controls")
        now = datetime.now(timezone.utc)
        return Preflight({key:"PASS" for key in REQUIRED_CHECKS}, SMTPProof("SMTP_ACCEPTED","REAL_SMTP",now),now)

    async def setup_intent(self, provider):
        item = product(provider)
        event = self.store.create_event(item,"RESTOCK",simulated=True)
        intent = self.intents.create(event,item)
        self.intents.set_cart(intent["intent_id"],"configuration_0")
        self.intents.mark_checkout_ready(intent["intent_id"])
        adapter = OfflineOrderProvider(self.path,provider)
        guard = RealOrderSmokeGuard(Path(self.temp.name) / "controls",mode="LIVE",provider=provider)
        guard.arm_smoke(await self.preflight(),intent["intent_id"],confirmed=True)
        self.preflight_calls = 0
        return intent["intent_id"],adapter,guard

    def runner(self, adapter, guard, *, allow_simulated=True):
        return PurchaseRunner(self.store,adapter,self.notifier,OfflineLog(),guard,self.preflight,
                              allow_simulated=allow_simulated)

    async def test_both_providers_commit_before_dispatch_notify_once_and_never_resubmit(self):
        for provider in ("bandwagon","dmit"):
            with self.subTest(provider=provider):
                identity,adapter,guard = await self.setup_intent(provider)
                runner = self.runner(adapter,guard)
                result = await runner.submit_checkout(identity,guard)
                self.assertEqual((result["status"],result["origin"],result["evidence_source"]),
                                 ("WAITING_FOR_USER","SIMULATED","MOCK"))
                self.assertEqual(result["notification"]["status"],"NOTIFICATION_FAILED")
                self.assertEqual(self.intents.get(identity)["state"],"WAITING_FOR_USER")
                self.assertEqual(self.notifier.calls.count(identity),1)
                self.assertEqual((adapter.prechecks,adapter.submits),(1,1))
                self.assertFalse(guard.status()["armed"])
                again = await runner.submit_checkout(identity,guard)
                self.assertEqual(again["error_code"],"NO_AUTOMATIC_RESUBMISSION")
                self.assertEqual(await self.runner(adapter,guard).recover(),[])
                self.assertEqual((adapter.prechecks,adapter.submits),(1,1))
                self.assertEqual(self.notifier.calls.count(identity),1)

    async def test_crash_after_commit_recovers_same_provider_order_without_resubmit_or_wrong_receipt(self):
        for provider in ("bandwagon","dmit"):
            with self.subTest(provider=provider):
                identity,adapter,guard = await self.setup_intent(provider)
                adapter.cancel_after_commit = True
                with self.assertRaises(asyncio.CancelledError):
                    await self.runner(adapter,guard).submit_checkout(identity,guard)
                saved_nonce = self.intents.get(identity)["submission_nonce"]
                self.assertEqual(self.intents.get(identity)["state"],"ORDER_UNCERTAIN")
                self.assertFalse(guard.status()["armed"])
                self.store.close(); self.store = Store(self.path); self.intents = IntentStore(self.store)
                adapter.lookup_changes = {"provider":"dmit" if provider == "bandwagon" else "bandwagon"}
                recovered = await self.runner(adapter,guard).recover()
                self.assertEqual(len(recovered),1)
                self.assertNotIn(recovered[0]["status"],{"PAYMENT_READY","WAITING_FOR_USER"})
                self.assertEqual(self.notifier.calls.count(identity),0)
                self.assertIsNone(self.intents.get(identity)["order_id"])
                adapter.lookup_changes = {}
                recovered = await self.runner(adapter,guard).recover()
                self.assertEqual(recovered[0]["status"],"WAITING_FOR_USER")
                saved = self.intents.get(identity)
                self.assertEqual((saved["order_id"],saved["invoice_id"],saved["provider"]),("123","456",provider))
                self.assertEqual(saved["submission_nonce"],saved_nonce)
                await self.runner(adapter,guard).submit_checkout(identity,guard)
                self.assertEqual((adapter.submits,self.notifier.calls.count(identity)),(1,1))

    async def test_kill_after_persist_then_verified_absence_does_not_allow_another_submit(self):
        identity,adapter,guard = await self.setup_intent("bandwagon")
        self.stop_after_persist = True
        runner = self.runner(adapter,guard)
        result = await runner.submit_checkout(identity,guard)
        self.assertEqual(result["status"],"ORDER_UNCERTAIN")
        self.assertEqual(adapter.submits,0)
        self.assertIsNotNone(self.intents.get(identity)["submission_nonce"])
        recovered = await runner.recover()
        self.assertEqual(recovered[0]["status"],"ORDER_SUBMIT_FAILED")
        self.assertEqual(recovered[0]["error_code"],"ORDER_ABSENCE_CONFIRMED_NO_AUTORETRY")
        await runner.submit_checkout(identity,guard)
        self.assertEqual((adapter.prechecks,adapter.submits),(1,0))
        self.assertEqual(self.notifier.calls,[])

    async def test_simulation_scope_provider_scope_and_no_charge_fail_before_dispatch(self):
        identity,adapter,guard = await self.setup_intent("dmit")
        with self.assertRaises(AutoGrabError):
            await self.runner(adapter,guard,allow_simulated=False).submit_checkout(identity,guard)
        other = RealOrderSmokeGuard(Path(self.temp.name)/"other",mode="LIVE",provider="bandwagon")
        other.arm_smoke(await self.preflight(),identity,confirmed=True)
        with self.assertRaises(AutoGrabError):
            await self.runner(adapter,other).submit_checkout(identity,other)
        self.assertEqual((adapter.prechecks,adapter.submits),(0,0))
        adapter.check_changes = {"no_charge_verified":False}
        with self.assertRaises((ValueError,AutoGrabError)):
            await self.runner(adapter,guard).submit_checkout(identity,guard)
        self.assertIsNone(self.intents.get(identity)["submission_nonce"])
        self.assertEqual(adapter.submits,0)
