"""UTC timestamps for records; monotonic nanoseconds for same-run durations."""

from datetime import datetime, timezone
from copy import deepcopy
import time
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Timing:
    def __init__(self, clock=time.monotonic_ns):
        self.run_id = uuid.uuid4().hex
        self.clock = clock
        self.marks: dict[str, dict] = {}

    def mark(self, stage: str) -> None:
        if stage not in {f"T{i}" for i in range(6)}:
            raise ValueError("Unknown timing stage")
        if stage in self.marks:
            raise ValueError("Timing stage already recorded")
        self.marks[stage] = {"utc": utc_now(), "monotonic_ns": self.clock()}

    def as_dict(self) -> dict:
        pairs = {"detection_to_verify": ("T0", "T1"), "verify_to_browser": ("T1", "T2"),
                 "browser_to_cart": ("T2", "T3"), "cart_to_boundary": ("T3", "T4"),
                 "detection_to_boundary": ("T0", "T4"), "detection_to_notification": ("T0", "T5")}
        durations = {}
        for name, (a, b) in pairs.items():
            durations[name] = round((self.marks[b]["monotonic_ns"] - self.marks[a]["monotonic_ns"]) / 1_000_000, 3) if a in self.marks and b in self.marks else None
        return {"run_id": self.run_id, "marks": deepcopy(self.marks), "durations_ms": durations,
                "T5_definition": "SMTP_ACCEPTED; inbox delivery not measured"}
