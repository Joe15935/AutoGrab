"""Purchase state graph and monotonic timing; all payment pages are fixtures."""

import unittest
from unittest.mock import patch

from autograb.core.errors import AutoGrabError
from autograb.core.purchase_state import PurchaseState, assert_payment_ready, transition
from autograb.core.purchase_timing import PurchaseTiming


def verified_payment():
    return {
        "order_id": "123", "invoice_id": "456", "product_id": "87",
        "payment_url": "https://bandwagonhost.com/viewinvoice.php?id=456",
        "merchant": "bandwagonhost.com", "invoice_status": "UNPAID", "source": "MOCK",
        "merchant_verified": True, "product_verified": True, "amount_present": True,
        "payment_page_verified": True, "unpaid_verified": True,
    }


class PurchaseStateTests(unittest.TestCase):
    def test_full_order_path_stops_waiting_for_user(self):
        current = PurchaseState.INTENT_CREATED
        for following in (
            "DETECTED", "VERIFYING", "PRODUCT_VERIFIED", "OPENING_BROWSER", "CART_READY",
            "CHECKOUT_READY", "ORDER_SUBMITTING", "ORDER_CREATED", "INVOICE_CREATED",
            "PAYMENT_URL_READY", "PAYMENT_READY", "WAITING_FOR_USER",
        ):
            current = transition(current, following, payment_verification=verified_payment())
        self.assertEqual(current, PurchaseState.WAITING_FOR_USER)

    def test_no_payment_or_card_authorization_state_exists(self):
        for name in ("PAY", "PAY_NOW", "PAYMENT_SUBMIT", "CARD_AUTHORIZATION", "PAID", "CONFIRM_PAYMENT"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                transition("WAITING_FOR_USER", name)

    def test_cannot_skip_order_invoice_or_payment_verification(self):
        for current, target in (("CART_READY", "ORDER_CREATED"), ("CHECKOUT_READY", "PAYMENT_READY"),
                                ("ORDER_CREATED", "PAYMENT_READY"), ("INVOICE_CREATED", "WAITING_FOR_USER")):
            with self.subTest(current=current, target=target), self.assertRaises(ValueError):
                transition(current, target, payment_verification=verified_payment())

    def test_unknown_submission_requires_reconciliation_without_retry(self):
        current = transition("ORDER_SUBMITTING", "RECONCILIATION_REQUIRED")
        with self.assertRaises(ValueError):
            transition(current, "CHECKOUT_READY")
        with self.assertRaises(ValueError):
            transition(current, "ORDER_SUBMITTING")
        current = transition(current, "ORDER_ALREADY_EXISTS")
        self.assertEqual(transition(current, "ORDER_CREATED"), PurchaseState.ORDER_CREATED)

    def test_confirmed_failure_does_not_autoretry(self):
        current = transition("RECONCILIATION_REQUIRED", "ORDER_SUBMIT_FAILED")
        with self.assertRaises(ValueError):
            transition(current, "ORDER_SUBMITTING")

    def test_existing_order_avoids_submit_state(self):
        current = transition("CHECKOUT_READY", "ORDER_ALREADY_EXISTS")
        self.assertEqual(transition(current, "ORDER_CREATED"), PurchaseState.ORDER_CREATED)

    def test_missing_invoice_or_payment_url_requires_reconciliation(self):
        for current, missing in (("ORDER_CREATED", "INVOICE_NOT_FOUND"), ("INVOICE_CREATED", "PAYMENT_URL_NOT_FOUND")):
            with self.subTest(missing=missing):
                state = transition(current, missing)
                self.assertEqual(transition(state, "RECONCILIATION_REQUIRED"), PurchaseState.RECONCILIATION_REQUIRED)

    def test_gateway_with_unknown_charge_semantics_waits_for_human(self):
        state = transition("PAYMENT_URL_READY", "PAYMENT_GATEWAY_SELECTION_REQUIRED")
        self.assertEqual(transition(state, "RECONCILIATION_REQUIRED"), PurchaseState.RECONCILIATION_REQUIRED)

    def test_human_or_provider_block_resumes_only_via_reconciliation(self):
        for name in ("LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "SESSION_EXPIRED", "PROVIDER_CHANGED"):
            with self.subTest(name=name):
                state = transition("ORDER_SUBMITTING", name)
                with self.assertRaises(ValueError):
                    transition(state, "ORDER_SUBMITTING")
                self.assertEqual(transition(state, "RECONCILIATION_REQUIRED"), PurchaseState.RECONCILIATION_REQUIRED)

    def test_expiry_and_cancellation_are_recorded_without_any_action(self):
        for target in ("ORDER_EXPIRED", "ORDER_CANCELLED"):
            with self.subTest(target=target):
                state = transition("WAITING_FOR_USER", target)
                with self.assertRaises(ValueError):
                    transition(state, "ORDER_SUBMITTING")

    def test_disarmed_or_failure_before_submit_is_terminal(self):
        for target in ("LIVE_NOT_ARMED", "FAILED"):
            with self.subTest(target=target):
                state = transition("CHECKOUT_READY", target)
                with self.assertRaises(ValueError):
                    transition(state, "ORDER_SUBMITTING")

    def test_payment_ready_requires_evidence(self):
        with self.assertRaises(AutoGrabError):
            transition("PAYMENT_URL_READY", "PAYMENT_READY")

    def test_payment_ready_requires_every_observed_property(self):
        evidence = verified_payment()
        for name in ("order_id", "invoice_id", "payment_url", "merchant_verified", "product_verified",
                     "amount_present", "payment_page_verified", "unpaid_verified"):
            with self.subTest(name=name):
                incomplete = {key: value for key, value in evidence.items() if key != name}
                with self.assertRaises(AutoGrabError):
                    assert_payment_ready(incomplete)

    def test_cart_login_external_or_wrong_invoice_url_cannot_be_payment_ready(self):
        for url in (
            "https://bandwagonhost.com/cart.php?a=view", "https://bandwagonhost.com/clientarea.php",
            "https://bandwagonhost.com/viewinvoice.php?id=999", "https://other.test/viewinvoice.php?id=456",
            "https://bandwagonhost.com/viewinvoice.php?id=456&token=fixture-value",
            "https://user:password@bandwagonhost.com/viewinvoice.php?id=456",
            "https://bandwagonhost.com/viewinvoice.php?id=456#pay", "http://bandwagonhost.com/viewinvoice.php?id=456",
            "https://bandwagonhost.com/viewinvoice.php?id=456\n", "https://bandwagonhost.com/viewinvoice.php?id=%34%35%36",
        ):
            with self.subTest(url=url), self.assertRaises(AutoGrabError):
                assert_payment_ready({**verified_payment(), "payment_url": url})

    def test_identifiers_and_true_values_are_strict(self):
        for update in ({"order_id": None}, {"invoice_id": 456}, {"order_id": "１２３"},
                       {"unpaid_verified": 1}, {"payment_page_verified": "true"}):
            with self.subTest(update=update), self.assertRaises(AutoGrabError):
                assert_payment_ready({**verified_payment(), **update})


class PurchaseTimingTests(unittest.TestCase):
    def test_all_ten_stages_and_report_intervals(self):
        ticks = iter(i * 1_111_111_000 for i in range(10))
        timing = PurchaseTiming(clock=lambda: next(ticks))
        for stage in (f"T{i}" for i in range(10)):
            timing.mark(stage)
        result = timing.as_dict()
        self.assertEqual(result["schema"], "PHASE_2_T0_T9")
        self.assertEqual(result["durations_ms"]["detection_to_cart"], 3333.333)
        self.assertEqual(result["durations_ms"]["detection_to_order"], 6666.666)
        self.assertEqual(result["durations_ms"]["detection_to_payment_ready"], 8888.888)
        self.assertEqual(result["durations_ms"]["detection_to_email"], 9999.999)
        self.assertEqual(result["stage_definitions"]["T5"], "ORDER_SUBMIT")
        self.assertEqual(result["stage_definitions"]["T9"], "EMAIL_ACCEPTED")
        self.assertIn("inbox delivery not measured", result["T9_definition"])

    def test_wall_clock_jumps_do_not_change_monotonic_durations(self):
        ticks = iter((1_000_000_000, 2_555_444_000))
        timing = PurchaseTiming(clock=lambda: next(ticks))
        with patch("autograb.core.purchase_timing.utc_now", side_effect=("2026-09-22T12:00:00+00:00", "2026-09-21T12:00:00+00:00")):
            timing.mark("T0")
            timing.mark("T8")
        self.assertEqual(timing.as_dict()["durations_ms"]["detection_to_payment_ready"], 1555.444)

    def test_missing_stages_are_unknown_never_invented(self):
        timing = PurchaseTiming(clock=lambda: 0)
        timing.mark("T0")
        self.assertTrue(all(value is None for value in timing.as_dict()["durations_ms"].values()))
        self.assertEqual(timing.as_dict()["cross_restart_durations"], "NOT_MEASURED")

    def test_duplicate_out_of_order_and_invalid_marks_rejected(self):
        timing = PurchaseTiming(clock=lambda: 0)
        timing.mark("T0")
        timing.mark("T3")
        for stage in ("T0", "T3", "T1", "T10", "EMAIL"):
            with self.subTest(stage=stage), self.assertRaises(ValueError):
                timing.mark(stage)

    def test_clock_reversal_rejected(self):
        ticks = iter((10, 9))
        timing = PurchaseTiming(clock=lambda: next(ticks))
        timing.mark("T0")
        with self.assertRaises(ValueError):
            timing.mark("T1")

    def test_output_cannot_mutate_saved_timing(self):
        timing = PurchaseTiming(clock=lambda: 123)
        timing.mark("T0")
        result = timing.as_dict()
        result["marks"]["T0"]["monotonic_ns"] = 999
        result["stage_definitions"]["T0"] = "CHANGED"
        self.assertEqual(timing.as_dict()["marks"]["T0"]["monotonic_ns"], 123)
        self.assertEqual(timing.as_dict()["stage_definitions"]["T0"], "DETECTED")


if __name__ == "__main__":
    unittest.main()
