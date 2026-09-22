"""Phase 2 timing; it never relabels Phase 1 T5 (SMTP) as an order submission."""

from copy import deepcopy
import time
import uuid

from .timing import utc_now


STAGES = {
    "T0": "DETECTED", "T1": "VERIFIED", "T2": "BROWSER_READY", "T3": "CART",
    "T4": "CHECKOUT", "T5": "ORDER_SUBMIT", "T6": "ORDER_CREATED",
    "T7": "PAYMENT_URL", "T8": "PAYMENT_READY", "T9": "EMAIL_ACCEPTED",
}


class PurchaseTiming:
    def __init__(self, clock=time.monotonic_ns):
        self.run_id = uuid.uuid4().hex
        self.clock = clock
        self.marks: dict[str, dict] = {}

    def mark(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError("Unknown Phase 2 timing stage")
        if stage in self.marks:
            raise ValueError("Timing stage already recorded")
        if self.marks and int(stage[1:]) <= max(int(existing[1:]) for existing in self.marks):
            raise ValueError("Timing stages must be recorded in increasing order")
        tick = self.clock()
        if self.marks and tick < next(reversed(self.marks.values()))["monotonic_ns"]:
            raise ValueError("Monotonic clock moved backwards")
        self.marks[stage] = {"utc": utc_now(), "monotonic_ns": tick}

    def as_dict(self) -> dict:
        pairs = {
            "detection_to_verify": ("T0", "T1"),
            "detection_to_cart": ("T0", "T3"),
            "detection_to_checkout": ("T0", "T4"),
            "detection_to_order": ("T0", "T6"),
            "detection_to_payment_url": ("T0", "T7"),
            "detection_to_payment_ready": ("T0", "T8"),
            "detection_to_email": ("T0", "T9"),
            "order_submit_to_created": ("T5", "T6"),
        }
        durations = {
            name: round((self.marks[end]["monotonic_ns"] - self.marks[start]["monotonic_ns"]) / 1_000_000, 3)
            if start in self.marks and end in self.marks else None
            for name, (start, end) in pairs.items()
        }
        return {
            "run_id": self.run_id,
            "schema": "PHASE_2_T0_T9",
            "stage_definitions": dict(STAGES),
            "marks": deepcopy(self.marks),
            "durations_ms": durations,
            "T9_definition": "SMTP_ACCEPTED; inbox delivery not measured",
            "cross_restart_durations": "NOT_MEASURED",
        }
