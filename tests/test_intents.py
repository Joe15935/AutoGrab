"""Offline durable order tests; no browser, account, order or payment access."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from autograb.core.models import Product
from autograb.storage.database import Store
from autograb.storage.intents import IntentConflict, IntentStore, validate_payment_url


def product(product_id="87"):
    return Product(product_id=product_id, name="Fixture VPS", availability="AVAILABLE",
                   prices=[{"cents": 4999, "period": "Annually", "currency": "USD"}],
                   product_url="https://bandwagonhost.com/order/ecommerce", eligible=True)


def verification(*, source="MOCK", product_id="87", invoice_id="456", order_id="123"):
    return {
        "order_id": order_id, "invoice_id": invoice_id,
        "payment_url": f"https://bandwagonhost.com/viewinvoice.php?id={invoice_id}",
        "merchant": "bandwagonhost.com", "product_id": product_id, "invoice_status": "UNPAID",
        "merchant_verified": True, "product_verified": True, "amount_present": True,
        "payment_page_verified": True, "unpaid_verified": True, "source": source,
        "amount": "$49.99 USD", "billing": "Annually", "deadline": None,
    }


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.intents = IntentStore(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def event(self, *, item=None, simulated=True):
        return self.store.create_event(item or product(), "RESTOCK", simulated=simulated)

    def create(self, *, item=None, simulated=True):
        item = item or product()
        return self.intents.create(self.event(item=item, simulated=simulated), item)

    def ready(self, *, item=None, simulated=True):
        intent = self.create(item=item, simulated=simulated)
        self.intents.set_cart(intent["intent_id"], "0")
        return self.intents.mark_checkout_ready(intent["intent_id"])

    def submitted(self, *, item=None, simulated=True):
        intent = self.ready(item=item, simulated=simulated)
        self.assertTrue(self.intents.mark_submitting(intent["intent_id"]))
        return self.intents.get(intent["intent_id"])

    def paid_ready(self, intent):
        evidence = verification(source="MOCK" if intent["origin"] == "SIMULATED" else "REAL_SITE",
                                product_id=intent["product_id"])
        return self.intents.persist_result(intent["intent_id"], order_id=evidence["order_id"],
                                          invoice_id=evidence["invoice_id"], payment_url=evidence["payment_url"],
                                          verification=evidence)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)
        self.intents = IntentStore(self.store)

    def test_additive_migration_preserves_baseline_products_events_and_notifications(self):
        # Remove the empty Phase 2 schema to start with exactly Phase 1 tables.
        self.store.connection.execute("DROP TABLE purchase_intents")
        self.store.ingest([replace(product(), availability="SOLD_OUT")])
        event = self.store.ingest([product()])["events"][0]
        self.store.record_notification(event["id"], "NOT_CONFIGURED")
        summary = deepcopy(self.store.summary())
        products, events = self.store.list_products(), self.store.list_events()
        self.reopen()
        IntentStore(self.store)
        self.assertEqual(self.store.summary(), summary)
        self.assertEqual(self.store.list_products(), products)
        self.assertEqual(self.store.list_events(), events)
        self.assertFalse(self.store.ingest([product()])["events"])

    def test_partial_order_is_in_recovery_queue_until_payment_page_verified(self):
        intent = self.submitted()
        identity = intent["intent_id"]
        for result in (dict(order_id="123"), dict(invoice_id="456"),
                       dict(payment_url=verification()["payment_url"])):
            self.intents.persist_result(identity, **result)
            self.reopen()
            self.assertEqual([item["intent_id"] for item in self.intents.recovery_intents()], [identity])
            self.assertFalse(self.intents.mark_submitting(identity))
        self.intents.persist_result(identity, verification=verification())
        self.assertEqual(self.intents.recovery_intents(), [])

    def test_create_has_explicit_origin_and_no_account_or_product_snapshot(self):
        for simulated, expected_origin in ((True, "SIMULATED"), (False, "REAL")):
            with self.subTest(simulated=simulated):
                created = self.create(item=product("87" if simulated else "88"), simulated=simulated)
                self.assertEqual(created["state"], "INTENT_CREATED")
                self.assertEqual(created["origin"], expected_origin)
                self.assertEqual(created["provider"], "bandwagon")
                self.assertIsNone(created["submit_started_at"])
                self.assertIsNone(created["payment_url"])
                self.assertIsNone(created["verification"])
                self.assertNotIn("product", created)
                self.assertNotIn("account", created)
                self.assertEqual(self.intents.get_by_event(created["event_id"]), created)

    def test_create_requires_persisted_matching_event_and_provider(self):
        event = self.event()
        for changed in ({**event, "id": "missing"}, {**event, "simulated": False},
                        {**event, "product_id": "88"}, {**event, "provider": "other"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.intents.create(changed, product())
        with self.assertRaises(ValueError):
            self.intents.create(event, replace(product(), provider="other"))
        self.assertEqual(self.intents.list(), [])

    def test_duplicate_event_rejected_for_lifetime_even_after_safe_abort(self):
        event = self.event()
        created = self.intents.create(event, product())
        self.intents.abort_before_submit(created["intent_id"])
        with self.assertRaises(IntentConflict) as error:
            self.intents.create(event, product())
        self.assertEqual(error.exception.code, "INTENT_ALREADY_EXISTS")
        self.assertEqual(error.exception.intent_id, created["intent_id"])
        self.assertEqual(len(self.intents.list()), 1)

    def test_active_product_blocks_different_event_and_opposite_origin(self):
        active = self.create(simulated=False)
        event = self.event(simulated=True)
        with self.assertRaises(IntentConflict) as error:
            self.intents.create(event, product())
        self.assertEqual(error.exception.code, "ACTIVE_INTENT_EXISTS")
        self.assertEqual(self.intents.active_for_product("87"), active)
        self.assertIsNone(self.intents.active_for_product("88"))

    def test_parallel_different_opportunities_have_only_one_active_product(self):
        event_ids = [self.event()["id"] for _ in range(4)]
        barrier = threading.Barrier(len(event_ids))

        def create(event_id):
            with Store(self.path) as store:
                intents = IntentStore(store)
                barrier.wait(timeout=15)
                try:
                    return intents.create(store.get_event(event_id), product())["intent_id"]
                except IntentConflict:
                    return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, event_ids))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(len(self.intents.list()), 1)

    def test_cart_and_checkout_transitions_do_not_grant_submit_early(self):
        created = self.create()
        identity = created["intent_id"]
        self.assertFalse(self.intents.mark_submitting(identity))
        with self.assertRaises(ValueError):
            self.intents.mark_checkout_ready(identity)
        with self.assertRaises(ValueError):
            self.intents.set_cart(identity, None)
        cart = self.intents.set_cart(identity, "0")
        self.assertEqual(cart["state"], "CART_READY")
        self.assertFalse(self.intents.mark_submitting(identity))
        with self.assertRaises(ValueError):
            self.intents.set_cart(identity, "1")
        self.intents.mark_checkout_ready(identity)
        self.assertTrue(self.intents.mark_submitting(identity))
        with self.assertRaises(ValueError):
            self.intents.set_cart(identity, "0")

    def test_submitting_is_a_durable_single_use_compare_and_set(self):
        identity = self.ready()["intent_id"]
        self.assertTrue(self.intents.mark_submitting(identity))
        saved = self.intents.get(identity)
        self.assertEqual(saved["state"], "ORDER_SUBMITTING")
        self.assertIsNotNone(saved["submit_started_at"])
        self.assertIsNone(saved["submit_finished_at"])
        self.reopen()
        self.assertEqual(self.intents.get(identity), saved)
        self.assertFalse(self.intents.mark_submitting(identity))

    def test_parallel_workers_receive_one_dispatch_permission(self):
        identity = self.ready()["intent_id"]
        barrier = threading.Barrier(4)

        def claim(_):
            with Store(self.path) as store:
                intents = IntentStore(store)
                barrier.wait(timeout=15)
                return intents.mark_submitting(identity)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(claim, range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 3)

    def test_crash_recovery_keeps_uncertainty_and_product_lock_across_restarts(self):
        identity = self.submitted()["intent_id"]
        self.reopen()
        recovered = self.intents.recover_interrupted()
        self.assertEqual([item["intent_id"] for item in recovered], [identity])
        self.assertEqual(recovered[0]["state"], "RECONCILIATION_REQUIRED")
        self.assertIsNone(recovered[0]["submit_finished_at"])
        self.assertFalse(self.intents.mark_submitting(identity))
        self.assertEqual(self.intents.recover_interrupted(), [])
        self.reopen()
        self.assertEqual(len(self.intents.recovery_intents()), 1)
        with self.assertRaises(IntentConflict):
            self.create()
        with self.assertRaises(ValueError):
            self.intents.abort_before_submit(identity)

    def test_timeout_before_server_commit_confirmed_absent_never_reposts(self):
        identity = self.submitted()["intent_id"]
        self.intents.mark_uncertain(identity)
        absent = self.intents.reconcile(identity, "ABSENT")
        self.assertEqual(absent["state"], "ORDER_SUBMIT_FAILED")
        self.assertEqual(absent["reconciliation_status"], "ABSENT")
        self.assertIsNotNone(absent["submit_finished_at"])
        self.reopen()
        self.assertFalse(self.intents.mark_submitting(identity))
        with self.assertRaises(ValueError):
            self.intents.mark_checkout_ready(identity)
        with self.assertRaises(IntentConflict):
            self.create()

    def test_timeout_after_server_commit_recovers_identifiers_without_second_dispatch(self):
        identity = self.ready()["intent_id"]
        dispatched = []
        if self.intents.mark_submitting(identity):
            dispatched.append("ONE_MOCK_SERVER_COMMIT")
        self.reopen()
        self.intents.recover_interrupted()
        evidence = verification()
        result = self.intents.reconcile(identity, "FOUND", order_id="123", invoice_id="456",
                                        payment_url=evidence["payment_url"], verification=evidence)
        if self.intents.mark_submitting(identity):
            dispatched.append("UNSAFE_SECOND_COMMIT")
        self.assertEqual(dispatched, ["ONE_MOCK_SERVER_COMMIT"])
        self.assertEqual(result["state"], "PAYMENT_READY")
        self.assertEqual(result["reconciliation_status"], "FOUND")

    def test_unknown_reconciliation_keeps_submission_uncertain(self):
        identity = self.submitted()["intent_id"]
        result = self.intents.reconcile(identity, "UNKNOWN")
        self.assertEqual(result["state"], "RECONCILIATION_REQUIRED")
        self.assertEqual(result["reconciliation_status"], "UNKNOWN")
        self.assertIsNone(result["order_id"])
        self.assertIsNone(result["submit_finished_at"])
        self.assertFalse(self.intents.mark_submitting(identity))

    def test_existing_order_can_be_reconciled_before_any_submission(self):
        identity = self.ready()["intent_id"]
        result = self.intents.reconcile(identity, "FOUND", order_id="123")
        self.assertEqual(result["state"], "ORDER_CREATED")
        self.assertIsNone(result["submit_started_at"])
        self.assertIsNone(result["submit_finished_at"])
        self.assertFalse(self.intents.mark_submitting(identity))

    def test_partial_results_progress_without_claiming_payment_ready(self):
        identity = self.submitted()["intent_id"]
        order = self.intents.persist_result(identity, order_id="123")
        self.assertEqual(order["state"], "ORDER_CREATED")
        invoice = self.intents.persist_result(identity, invoice_id="456")
        self.assertEqual(invoice["state"], "INVOICE_CREATED")
        linked = self.intents.persist_result(identity, payment_url=verification()["payment_url"])
        self.assertEqual(linked["state"], "PAYMENT_URL_READY")
        self.assertFalse(linked["payment_page_verified"])
        with self.assertRaises(ValueError):
            self.intents.mark_waiting(identity)
        verified = self.intents.persist_result(identity, verification=verification())
        self.assertEqual(verified["state"], "PAYMENT_READY")
        waiting = self.intents.mark_waiting(identity)
        self.assertEqual(waiting["state"], "WAITING_FOR_USER")
        self.reopen()
        self.assertEqual(self.intents.get(identity), waiting)
        self.assertEqual(waiting["order_id"], "123")
        self.assertEqual(waiting["invoice_id"], "456")
        self.assertEqual(waiting["payment_url"], verification()["payment_url"])

    def test_payment_boolean_or_url_alone_is_insufficient(self):
        identity = self.submitted()["intent_id"]
        before = self.intents.get(identity)
        with self.assertRaises(ValueError):
            self.intents.persist_result(identity, order_id="123", invoice_id="456",
                                        payment_url=verification()["payment_url"], payment_page_verified=True)
        self.assertEqual(self.intents.get(identity), before)
        with self.assertRaises(ValueError):
            self.intents.persist_result(identity, order_id="123", payment_url=verification()["payment_url"])
        self.assertEqual(self.intents.get(identity), before)

    def test_every_payment_evidence_check_is_mandatory_and_atomic(self):
        identity = self.submitted()["intent_id"]
        evidence = verification()
        for field in evidence:
            if field in {"amount", "billing", "deadline"}:
                continue
            for malformed in ({key: value for key, value in evidence.items() if key != field},
                              {**evidence, field: False}):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    self.intents.persist_result(identity, order_id="123", invoice_id="456",
                                                payment_url=evidence["payment_url"], verification=malformed)
            self.assertEqual(self.intents.get(identity)["state"], "ORDER_SUBMITTING")
            self.assertIsNone(self.intents.get(identity)["order_id"])

    def test_mock_evidence_never_promotes_real_intent(self):
        intent = self.submitted(simulated=False)
        with self.assertRaises(ValueError):
            self.intents.persist_result(intent["intent_id"], order_id="123", invoice_id="456",
                                        payment_url=verification()["payment_url"], verification=verification())
        # This is still an offline test of the evidence contract, not real order evidence.
        result = self.paid_ready(intent)
        self.assertEqual(result["verification"]["source"], "REAL_SITE")

    def test_verification_rejects_private_extra_fields_and_unstructured_text(self):
        identity = self.submitted()["intent_id"]
        changes = [dict(account_email="fixture@example.test"), dict(cookie="fixture"),
                   dict(amount="Contact owner@example.test"), dict(billing="arbitrary account detail"),
                   dict(deadline="ask support"), dict(invoice_status="PAID"), dict(invoice_status="EXPIRED"),
                   dict(product_id="88"), dict(invoice_id="999"), dict(merchant="other.test")]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.intents.persist_result(identity, order_id="123", invoice_id="456",
                                            payment_url=verification()["payment_url"],
                                            verification={**verification(), **change})

    def test_recorded_identifiers_cannot_be_rebound_to_another_order(self):
        identity = self.submitted()["intent_id"]
        self.intents.persist_result(identity, order_id="123", invoice_id="456")
        for changes in (dict(order_id="124"), dict(invoice_id="457"),
                        dict(payment_url="https://bandwagonhost.com/viewinvoice.php?id=457")):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.intents.persist_result(identity, **changes)
        with self.assertRaises(ValueError):
            self.intents.reconcile(identity, "ABSENT")
        self.assertEqual(self.intents.get(identity)["order_id"], "123")

    def test_known_order_evidence_survives_inconclusive_later_lookup(self):
        intent = self.submitted()
        ready = self.paid_ready(intent)
        unknown = self.intents.reconcile(intent["intent_id"], "UNKNOWN")
        self.assertEqual(unknown["order_id"], ready["order_id"])
        self.assertEqual(unknown["payment_url"], ready["payment_url"])
        self.assertEqual(unknown["state"], ready["state"])
        self.assertEqual(unknown["reconciliation_status"], "UNKNOWN")
        self.assertFalse(self.intents.mark_submitting(intent["intent_id"]))

    def test_only_observed_expiry_or_cancellation_releases_product(self):
        intent = self.submitted()
        self.paid_ready(intent)
        for state, confirmed in (("ORDER_EXPIRED", False), ("ORDER_CANCELLED", False), ("PAID", True)):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.intents.record_terminal(intent["intent_id"], state, server_confirmed=confirmed)
        expired = self.intents.record_terminal(
            intent["intent_id"], "ORDER_EXPIRED", server_confirmed=True,
            terminal_evidence={"provider": "bandwagon", "product_id": "87", "order_id": "123",
                               "invoice_id": "456", "source": "MOCK", "status": "ORDER_EXPIRED"},
        )
        self.assertEqual(expired["state"], "ORDER_EXPIRED")
        self.assertEqual(expired["order_id"], "123")
        self.assertIsNone(self.intents.active_for_product("87"))
        replacement = self.create()
        self.assertNotEqual(replacement["intent_id"], intent["intent_id"])
        with self.assertRaises(IntentConflict):
            self.intents.create(self.store.get_event(intent["event_id"]), product())

    def test_list_filters_and_stable_get_missing(self):
        first = self.create(simulated=False)
        second = self.create(item=product("88"))
        self.intents.abort_before_submit(first["intent_id"])
        self.assertEqual(self.intents.list(active_only=True), [second])
        self.assertEqual(len(self.intents.list(origin="REAL")), 1)
        self.assertEqual(len(self.intents.list(origin="SIMULATED")), 1)
        self.assertEqual(len(self.intents.list(limit=1)), 1)
        self.assertIsNone(self.intents.get("missing"))
        with self.assertRaises(KeyError):
            self.intents.mark_submitting("missing")
        for options in (dict(limit=0), dict(limit=True), dict(origin="LIVE"), dict(active_only=1)):
            with self.assertRaises(ValueError):
                self.intents.list(**options)

    def test_reconciliation_rejects_conflicting_or_unspecified_evidence(self):
        identity = self.submitted()["intent_id"]
        for status, changes in (("MAYBE", {}), ("FOUND", {}), ("ABSENT", dict(order_id="123")),
                                ("UNKNOWN", dict(verification=verification()))):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.intents.reconcile(identity, status, **changes)
        self.assertEqual(self.intents.get(identity)["state"], "ORDER_SUBMITTING")


class PaymentURLTests(unittest.TestCase):
    def test_official_invoice_link_is_canonical_and_invoice_specific(self):
        url = "https://bandwagonhost.com/viewinvoice.php?id=456"
        self.assertEqual(validate_payment_url(url, "456"), url)
        self.assertIsNone(validate_payment_url(None))
        with self.assertRaises(ValueError):
            validate_payment_url(url, "457")

    def test_cart_redirect_payment_token_or_external_link_is_rejected(self):
        for url in (
            "https://bandwagonhost.com/cart.php?a=checkout", "https://paypal.com/viewinvoice.php?id=456",
            "http://bandwagonhost.com/viewinvoice.php?id=456", "https://bandwagonhost.com:443/viewinvoice.php?id=456",
            "https://user@bandwagonhost.com/viewinvoice.php?id=456", "https://bandwagonhost.com.evil.test/viewinvoice.php?id=456",
            "https://bandwagonhost.com/viewinvoice.php?id=456&token=fixture", "https://bandwagonhost.com/viewinvoice.php?id=456#token",
            "https://bandwagonhost.com/viewinvoice.php?id=456&id=456", "https://bandwagonhost.com/viewinvoice.php?id=0",
            "https://bandwagonhost.com/viewinvoice.php?id=-1", "https://bandwagonhost.com/viewinvoice.php?id=0456",
            "https://bandwagonhost.com/viewinvoice.php?%69d=456", "https://bandwagonhost.com/viewinvoice.php?id=%34%35%36",
            "https://bandwagonhost.com/viewinvoice.php?id=456&", "https://bandwagonhost.com/viewinvoice.php?id=456\n",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_payment_url(url)


if __name__ == "__main__":
    unittest.main()
