"""Bounded launch scheduling with sleeping, not spin-waiting."""
import asyncio
from datetime import datetime
import time

from .errors import AutoGrabError
from .live import signal_present

NORMAL = {"bandwagon": 300, "dmit": 900, "vmiss": 900, "vps": 1800, "apple": 60}
BURST = {"bandwagon": 30, "dmit": 300, "vmiss": 900, "vps": 300, "apple": 60}


def launch_timestamp(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("--launch-at requires an explicit UTC offset")
    return moment.timestamp()


async def wait_until(target, *, data_dir, warm=None, clock=time.time, sleep=asyncio.sleep):
    warmed = False
    while True:
        if signal_present(data_dir, "stop_monitoring"):
            raise AutoGrabError("MONITORING_STOPPED")
        remaining = target - clock()
        if not warmed and remaining <= 30:
            if warm:
                await warm()
            warmed = True
            remaining = target - clock()
        if remaining <= 0:
            return
        # Re-read wall time after every sleep. Never start HTTP before T0.
        boundary = remaining - 30 if remaining > 30 else remaining
        await sleep(min(30, boundary) if remaining > 1 else min(0.05, remaining))
