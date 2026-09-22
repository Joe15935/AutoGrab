"""Small, explicit Phase 1 state graph. There are no live transaction states."""

from enum import StrEnum


class State(StrEnum):
    IDLE = "IDLE"
    MONITORING = "MONITORING"
    BASELINE = "BASELINE"
    DETECTED = "DETECTED"
    VERIFYING = "VERIFYING"
    PRODUCT_VERIFIED = "PRODUCT_VERIFIED"
    OPENING_BROWSER = "OPENING_BROWSER"
    CART_READY = "CART_READY"
    DRY_RUN_BOUNDARY_REACHED = "DRY_RUN_BOUNDARY_REACHED"
    NOTIFYING = "NOTIFYING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"


EDGES = {
    State.IDLE: {State.MONITORING, State.DETECTED},
    State.MONITORING: {State.BASELINE, State.DETECTED},
    State.BASELINE: {State.MONITORING},
    State.DETECTED: {State.VERIFYING},
    State.VERIFYING: {State.PRODUCT_VERIFIED},
    State.PRODUCT_VERIFIED: {State.OPENING_BROWSER},
    State.OPENING_BROWSER: {State.CART_READY},
    State.CART_READY: {State.DRY_RUN_BOUNDARY_REACHED},
    State.DRY_RUN_BOUNDARY_REACHED: {State.NOTIFYING},
    State.NOTIFYING: {State.COMPLETE},
}


def transition(current: State | str, target: State | str) -> State:
    current, target = State(current), State(target)
    terminal = {State.COMPLETE, State.FAILED, State.CAPTCHA_REQUIRED, State.LOGIN_REQUIRED}
    if current not in terminal and target in {State.FAILED, State.CAPTCHA_REQUIRED, State.LOGIN_REQUIRED}:
        return target
    if target not in EDGES.get(current, set()):
        raise ValueError(f"Invalid state transition: {current} -> {target}")
    return target
