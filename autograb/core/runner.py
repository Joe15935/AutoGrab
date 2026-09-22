"""Durable single-event workflow. A failed mutation is never retried."""
import asyncio
from dataclasses import asdict

from .errors import AutoGrabError
from .models import Product
from .state import State, transition
from .timing import Timing
from autograb.notifications.email import NotificationResult


class Runner:
    def __init__(self, store, provider, notifier, log):
        self.store, self.provider, self.notifier, self.log = store, provider, notifier, log

    async def process(self, event):
        if not self.store.claim_event(event["id"]):
            return {"status": "ALREADY_CLAIMED", "event_id": event["id"]}
        # The claimed, persisted identity is authoritative, not a caller's copy.
        event = self.store.get_event(event["id"])
        timing, state, timeline = Timing(), State.IDLE, []
        timing.mark("T0")
        boundary = None

        def advance(target, **extra):
            nonlocal state
            state = transition(state, target)
            timeline.append(state.value)
            self.store.update_event(event["id"], state.value, {"timing": timing.as_dict(), "timeline": timeline.copy(), **extra})
            self.log.write("STATE", event_id=event["id"], product_id=event["product_id"],
                           event_type=event["event_type"], simulated=event["simulated"], state=state.value)

        try:
            advance(State.DETECTED)
            advance(State.VERIFYING)
            product = await self.provider.check_product(event["product_id"])
            if not isinstance(product, Product):
                raise AutoGrabError("PRODUCT_UNVERIFIED")
            if (product.provider, product.product_id) != (event["provider"], event["product_id"]):
                raise AutoGrabError("PRODUCT_MISMATCH")
            if product.availability == "SOLD_OUT":
                raise AutoGrabError("OUT_OF_STOCK")
            if product.availability != "AVAILABLE":
                raise AutoGrabError("STOCK_UNKNOWN")
            timing.mark("T1")
            advance(State.PRODUCT_VERIFIED)
            advance(State.OPENING_BROWSER)
            await self.provider.open_product(product)
            timing.mark("T2")
            cart = await self.provider.prepare_cart(product)
            timing.mark("T3")
            advance(State.CART_READY, cart=cart)
            boundary = await self.provider.dry_run_checkout(product, cart)
            if (
                not isinstance(boundary, dict)
                or boundary.get("status") != State.DRY_RUN_BOUNDARY_REACHED
                or boundary.get("order_created") is not False
                or boundary.get("payment_url") is not None
            ):
                raise AutoGrabError("BOUNDARY_UNVERIFIED")
            timing.mark("T4")
            advance(State.DRY_RUN_BOUNDARY_REACHED, boundary=boundary)
            advance(State.NOTIFYING)
            try:
                notification = await self.notifier.send_event(product, event, timing.as_dict(), boundary)
                if not isinstance(notification, NotificationResult):
                    raise TypeError("Unverified notification result")
            except Exception:
                notification = NotificationResult("NOTIFICATION_FAILED", "NOTIFIER_ERROR", "Notification failed; product event is preserved.")
            if notification.status == "SMTP_ACCEPTED":
                timing.mark("T5")
            self.store.record_notification(event["id"], notification.status, notification.error_code or notification.detail)
            advance(State.COMPLETE, notification=asdict(notification))
            result = {"event_id": event["id"], "status": state.value, "simulated": event["simulated"],
                      "event_type": event["event_type"], "boundary": boundary,
                      "notification": asdict(notification), "timing": timing.as_dict()}
            self.log.write("RESULT", **result)
            return result
        except asyncio.CancelledError:
            self.store.update_event(event["id"], "INTERRUPTED", {"timing": timing.as_dict(), "timeline": timeline.copy()})
            raise
        except Exception as error:
            code = error.code if isinstance(error, AutoGrabError) else "UNKNOWN"
            target = State.CAPTCHA_REQUIRED if code in {"CAPTCHA_REQUIRED", "CAPTCHA", "CLOUDFLARE"} else State.LOGIN_REQUIRED if code == "LOGIN_REQUIRED" else State.FAILED
            advance(target, error_code=code)
            result = {"event_id": event["id"], "status": state.value, "error_code": code,
                      "simulated": event["simulated"], "timing": timing.as_dict()}
            self.log.write("RESULT", **result)
            return result
