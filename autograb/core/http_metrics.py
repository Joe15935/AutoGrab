"""Count public HTTP attempts inside one run, including failed requests."""
from contextvars import ContextVar
from dataclasses import dataclass
import time

from .errors import AutoGrabError


@dataclass
class RequestMeter:
    count: int = 0
    deadline: float | None = None


current_meter = ContextVar("autograb_http_meter", default=None)


def request_started():
    meter = current_meter.get()
    if meter is not None:
        if meter.deadline is not None and time.time() >= meter.deadline:
            raise AutoGrabError("LAUNCH_WINDOW_ENDED")
        meter.count += 1
