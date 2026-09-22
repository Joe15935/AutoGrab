"""Targeted safety regressions; every source assertion here is synthetic.

Even REAL / REAL_SITE fixture labels test provenance rejection only. All state
is in temporary SQLite databases; there is no merchant, account, mail or network.
"""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from autograb.core.live import LiveGuard
from autograb.core.models import Product
from autograb.core.purchase import PurchaseRunner
from autograb.notifications.email import NotificationResult
from autograb.storage.database import Store
from autograb.storage.intents import IntentConflict, IntentStore


def product():
    return Product("87", "Synthetic review VPS", "AVAILABLE", [],
                   "https://bandwagonhost.com/order/ecommerce", eligible=True)


def verification(source="MOCK"):
    return {
        "order_id": "123", "invoice_id": "456",
        "payment_url": "https://bandwagonhost.com/viewinvoice.php?id=456",
        "merchant": "bandwagonhost.com", "product_id": "87", "invoice_status": "UNPAID",
        "merchant_verified": True, "product_verified": True, "amount_present": True,
        "payment_page_verified": True, "unpaid_verified": True, "source": source,
        "amount": "$49.99 USD",
    }


def terminal(source="MOCK", state="ORDER_EXPIRED"):
    return {"provider": "bandwagon", "product_id": "87", "order_id": "123", "invoice_id": "456",
            "source": source, "status": state}


class IntentSafetyReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.intents = IntentStore(self.store)

    def submitted(self, simulated=True):
        event = self.store.create_event(product(), "RESTOCK", simulated=simulated)
        intent = self.intents.create(event, product())
        self.intents.set_cart(intent["intent_id"], "0")
        self.intents.mark_checkout_ready(intent["intent_id"])
        self.assertTrue(self.intents.mark_submitting(intent["intent_id"]))
        return self.intents.get(intent["intent_id"])

    def ready(self, simulated=True):
        intent = self.submitted(simulated)
        evidence = verification("MOCK" if simulated else "REAL_SITE")
        return self.intents.persist_result(
            intent["intent_id"], order_id="123", invoice_id="456", payment_url=evidence["payment_url"],
            verification=evidence,
        )

    def test_boolean_terminal_confirmation_cannot_release_active_order(self):
        intent = self.ready()
        with self.assertRaises(ValueError):
            self.intents.record_terminal(intent["intent_id"], "ORDER_EXPIRED", server_confirmed=True)
        self.assertEqual(self.intents.get(intent["intent_id"]), intent)
        self.assertEqual(self.intents.active_for_product("87")["intent_id"], intent["intent_id"])

    def test_every_terminal_identity_and_origin_field_is_required(self):
        intent = self.ready()
        correct = terminal()
        invalid = [{k: v for k, v in correct.items() if k != field} for field in correct]
        invalid.extend({**correct, field: value} for field, value in (
            ("provider", "other"), ("product_id", "88"), ("order_id", "999"),
            ("invoice_id", "999"), ("source", "REAL_SITE"), ("status", "ORDER_CANCELLED"),
        ))
        invalid.append({**correct, "private_account_value": "fixture-sensitive"})
        for evidence in invalid:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.intents.record_terminal(intent["intent_id"], "ORDER_EXPIRED", server_confirmed=True,
                                             terminal_evidence=evidence)
            self.assertEqual(self.intents.get(intent["intent_id"]), intent)

    def test_real_order_cannot_be_released_by_simulated_terminal_evidence(self):
        intent = self.ready(simulated=False)
        for source in ("MOCK", "SIMULATED"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.intents.record_terminal(intent["intent_id"], "ORDER_EXPIRED", server_confirmed=True,
                                             terminal_evidence=terminal(source))
        self.assertIsNotNone(self.intents.active_for_product("87"))

    def test_verified_terminal_evidence_persists_and_revokes_payment_ready(self):
        intent = self.ready()
        recorded = self.intents.record_terminal(intent["intent_id"], "ORDER_EXPIRED", server_confirmed=True,
                                                terminal_evidence=terminal())
        self.assertEqual(recorded["terminal_evidence"], terminal())
        self.assertFalse(recorded["payment_page_verified"])
        self.assertEqual(recorded["order_id"], "123")
        self.assertIsNone(self.intents.active_for_product("87"))
        self.store.close()
        self.store = Store(self.path)
        self.intents = IntentStore(self.store)
        self.assertEqual(self.intents.get(intent["intent_id"]), recorded)
        self.assertFalse(self.intents.mark_submitting(intent["intent_id"]))
        event = self.store.create_event(product(), "RESTOCK", simulated=True)
        self.assertNotEqual(self.intents.create(event, product())["intent_id"], intent["intent_id"])

    def test_uncertain_submission_cannot_be_expired_without_observed_order(self):
        intent = self.submitted()
        self.intents.mark_uncertain(intent["intent_id"])
        with self.assertRaises(ValueError):
            self.intents.record_terminal(intent["intent_id"], "ORDER_EXPIRED", server_confirmed=True,
                                         terminal_evidence=terminal())
        event = self.store.create_event(product(), "RESTOCK", simulated=True)
        with self.assertRaises(IntentConflict):
            self.intents.create(event, product())

    def test_verified_cancellation_only_records_it_and_keeps_event_deduplication(self):
        intent = self.ready()
        self.intents.record_terminal(intent["intent_id"], "ORDER_CANCELLED", server_confirmed=True,
                                     terminal_evidence=terminal(state="ORDER_CANCELLED"))
        self.assertIsNone(self.intents.active_for_product("87"))
        with self.assertRaises(IntentConflict):
            self.intents.create(self.store.get_event(intent["event_id"]), product())
        with self.assertRaises(ValueError):
            self.intents.record_terminal(intent["intent_id"], "ORDER_EXPIRED", server_confirmed=True,
                                         terminal_evidence=terminal())

    def test_terminal_evidence_migration_preserves_existing_purchase_records(self):
        intent = self.ready()
        self.store.connection.execute("ALTER TABLE purchase_intents DROP COLUMN terminal_evidence_json")
        self.intents = IntentStore(self.store)
        self.assertEqual(self.intents.get(intent["intent_id"]), intent)

    def test_payment_ready_rejects_later_opposite_origin_evidence_atomically(self):
        intent = self.ready(simulated=False)
        with self.assertRaises(ValueError):
            self.intents.persist_result(intent["intent_id"], verification=verification("MOCK"))
        self.assertEqual(self.intents.get(intent["intent_id"]), intent)


class RecoverySafetyReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = Store(self.root / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.intents = IntentStore(self.store)
        event = self.store.create_event(product(), "RESTOCK", simulated=False)
        intent = self.intents.create(event, product())
        self.intents.set_cart(intent["intent_id"], "0")
        self.intents.mark_checkout_ready(intent["intent_id"])
        self.intents.mark_submitting(intent["intent_id"])
        self.evidence = verification("REAL_SITE")
        self.intent = self.intents.persist_result(
            intent["intent_id"], order_id="123", invoice_id="456", payment_url=self.evidence["payment_url"],
            verification=self.evidence,
        )
        self.provider = SimpleNamespace(
            reconcile_intent=AsyncMock(return_value={"status": "FOUND", "order_id": "123",
                                                    "invoice_id": "456", "payment_url": self.evidence["payment_url"]}),
            submit_unpaid_order=AsyncMock(),
        )
        self.notifier = SimpleNamespace(send_payment_ready=AsyncMock(return_value=NotificationResult("SMTP_ACCEPTED")))
        self.runner = PurchaseRunner(self.store, self.provider, self.notifier, SimpleNamespace(write=Mock()),
                                     LiveGuard(self.root), AsyncMock())

    async def test_recovery_requires_fresh_unpaid_page_evidence_before_new_pay_now_email(self):
        # Historical PAYMENT_READY survived a crash. Current lookup finds IDs
        # only, not proof the invoice remains unpaid and payable now.
        results = await self.runner.recover()
        self.provider.submit_unpaid_order.assert_not_awaited()
        self.notifier.send_payment_ready.assert_not_awaited()
        self.assertTrue(results)
        self.assertNotEqual(results[0]["status"], "WAITING_FOR_USER")
        self.assertIsNotNone(self.intents.active_for_product("87"))
        self.assertEqual(self.intents.get(self.intent["intent_id"])["order_id"], "123")


if __name__ == "__main__":
    unittest.main()
