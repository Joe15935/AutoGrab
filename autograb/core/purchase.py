"""Single-worker unpaid-order coordination, with durable uncertainty recovery.

The provider adapter must establish its real checkout boundary independently.
No payment operation is part of this interface. Callers must hold AutoGrab's
process/profile lock; the asyncio lock additionally serializes opportunities in
the current worker. Normal operation rejects simulated events completely.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .errors import AutoGrabError
from .live import LiveGuard, Preflight, RealOrderSmokeGuard
from .models import Product
from .purchase_state import PurchaseState, transition
from .purchase_timing import PurchaseTiming
from autograb.notifications.email import NotificationResult
from autograb.storage.intents import IntentConflict, IntentStore


TRIGGER_TYPES = frozenset({"NEW_PRODUCT", "RESTOCK", "PROMOTIONAL_EVENT"})
HUMAN_ERRORS = frozenset({"LOGIN_REQUIRED", "SESSION_EXPIRED", "CAPTCHA_REQUIRED"})


def _error_code(error: BaseException) -> str:
    value = error.code if isinstance(error, AutoGrabError) else "PROVIDER_OPERATION_FAILED"
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value) else "PROVIDER_OPERATION_FAILED"


def _existing_status(event, intent) -> str:
    requested_origin = "SIMULATED" if event["simulated"] else "REAL"
    if intent["origin"] == requested_origin:
        return "ORDER_ALREADY_EXISTS"
    return "SIMULATED_INTENT_CONFLICT" if intent["origin"] == "SIMULATED" else "INTENT_ORIGIN_CONFLICT"


def _unverified_status(intent) -> str:
    # Historical readiness survives in storage for recovery, but an inconclusive
    # current lookup must never be reported as a newly verified payment page.
    return "RECONCILIATION_REQUIRED" if intent["state"] in {"PAYMENT_READY", "WAITING_FOR_USER"} else intent["state"]


class PurchaseRunner:
    def __init__(self, store, provider, notifier, log, guard: LiveGuard, preflight,
                 *, allow_simulated: bool = False):
        if type(allow_simulated) is not bool:
            raise ValueError("Simulation permission must be explicit")
        if allow_simulated and getattr(provider, "simulation_only", False) is not True:
            raise ValueError("Simulated purchases require an explicitly offline provider")
        self.store, self.provider, self.notifier, self.log = store, provider, notifier, log
        self.guard, self.preflight = guard, preflight
        self.intents = IntentStore(store)
        self.allow_simulated = allow_simulated
        self._worker = asyncio.Lock()
        # A fresh LIVE worker must not consume a prior process's PENDING queue.
        # Row identity avoids millisecond timestamp races and wall-clock drift.
        self._event_rowid_watermark = self.store.connection.execute(
            "SELECT COALESCE(MAX(rowid), 0) FROM events"
        ).fetchone()[0]

    def _simulation_allowed(self) -> bool:
        # Recheck at use time too, so an adapter changed/replaced after worker
        # construction cannot inherit the offline fixture exception.
        return self.allow_simulated and getattr(self.provider, "simulation_only", False) is True

    def _check_origin(self, event) -> None:
        if event["simulated"] and not self._simulation_allowed():
            raise AutoGrabError("SIMULATED_EVENT_CANNOT_ORDER")

    async def _check_guard(self) -> None:
        evidence = await self.preflight()
        if not isinstance(evidence, Preflight):
            raise AutoGrabError("LIVE_PREFLIGHT_FAILED")
        session_status = evidence.checks.get("session")
        if session_status in HUMAN_ERRORS:
            self.guard.disarm("PREFLIGHT_FAILED")
            raise AutoGrabError(session_status)
        self.guard.assert_can_submit(evidence)

    def _record(self, event, status, timing, timeline, *, intent_id=None, error_code=None, notification=None):
        details = {"purchase_timing": timing.as_dict(), "purchase_timeline": list(timeline)}
        if intent_id is not None:
            details["intent_id"] = intent_id
        if error_code is not None:
            details["purchase_error_code"] = error_code
        if notification is not None:
            details["purchase_notification"] = notification
        self.store.update_event(event["id"], str(status), details)
        self.log.write("PURCHASE_STATE", event_id=event["id"], state=str(status),
                       product_id=event["product_id"], origin="SIMULATED" if event["simulated"] else "REAL",
                       intent_id=intent_id, error_code=error_code)

    def _result(self, event, status, timing, *, intent=None, error_code=None, notification=None, recovery=False):
        request_origin = "SIMULATED" if event["simulated"] else "REAL"
        outcome_origin = intent["origin"] if intent is not None else request_origin
        verification = intent.get("verification") if intent is not None else None
        result = {
            "event_id": event["id"], "status": str(status),
            "origin": outcome_origin, "request_origin": request_origin,
            "evidence_source": verification["source"] if verification is not None else "NOT_VERIFIED",
            "timing": timing.as_dict(), "recovery": recovery,
            "automatic_payment": "DISABLED",
        }
        if intent is not None:
            result["intent_id"] = intent["intent_id"]
            result["intent_state"] = intent["state"]
        if error_code is not None:
            result["error_code"] = error_code
        if notification is not None:
            result["notification"] = notification
        self.log.write("PURCHASE_RESULT", **result)
        return result

    async def _notify_human(self, event, code):
        if code not in HUMAN_ERRORS:
            return
        try:
            provider = event.get('provider', 'bandwagon')
            result = await self.notifier.send_session_required(event["id"], code,
                **({'provider': provider} if provider != 'bandwagon' else {}))
            if not isinstance(result, NotificationResult):
                raise TypeError("Invalid notifier result")
            self.store.record_notification(event["id"], result.status, code)
        except Exception:
            self.store.record_notification(event["id"], "NOTIFICATION_FAILED", "SESSION_NOTICE_FAILED")

    async def _notify_payment(self, event, product, intent, timing):
        # PAYMENT_READY was committed before even entering the notifier. A
        # transport failure cannot remove the order or cause another dispatch.
        if not self.intents.claim_payment_notification(intent["intent_id"]):
            return self.intents.mark_waiting(intent["intent_id"]), {"status": "ALREADY_CLAIMED", "error_code": None}
        try:
            result = await self.notifier.send_payment_ready(product, event, intent, timing.as_dict())
            if not isinstance(result, NotificationResult):
                raise TypeError("Invalid notifier result")
        except Exception:
            result = NotificationResult("NOTIFICATION_FAILED", "NOTIFIER_ERROR")
        status = result.status if result.status in {"SMTP_ACCEPTED", "NOT_CONFIGURED", "NOTIFICATION_FAILED"} else "NOTIFICATION_FAILED"
        code = result.error_code
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code)):
            code = "NOTIFIER_ERROR"
        notification = {"status": status, "error_code": code}
        if status == "SMTP_ACCEPTED":
            timing.mark("T9")
        self.store.record_notification(event["id"], status, code or "")
        waiting = self.intents.mark_waiting(intent["intent_id"])
        return waiting, notification

    async def precheck_checkout(self, intent_id):
        """Read the existing L5 page, without repeating product/cart actions."""
        intent = self.intents._required(intent_id)
        if intent["provider"] not in {"bandwagon", "dmit"} or intent["submit_started_at"] is not None:
            raise AutoGrabError("ORDER_RECONCILIATION_REQUIRED")
        event = self.store.get_event(intent["event_id"])
        product = Product.from_dict(event["product"])
        observation = await self.provider.precheck_order(intent, product)
        if not isinstance(observation, dict) or observation.get("outcome") != "PRECHECK_READY":
            return observation
        self.intents.set_order_precheck(intent_id, observation)
        return observation

    async def submit_checkout(self, intent_id, smoke_guard):
        """Shared BWH/DMIT L6/L7 path, consuming one separately armed permit.

        A normal monitoring guard, simulated browser, environment flag or prior
        process cannot authorize this entry point. Existing L5 evidence is used;
        only its current final boundary is read again immediately before submit.
        """
        async with self._worker:
            intent = self.intents._required(intent_id)
            event = self.store.get_event(intent["event_id"])
            product = Product.from_dict(event["product"])
            timing = PurchaseTiming()
            if intent["submit_started_at"] is not None:
                return self._result(event, "ORDER_UNCERTAIN" if not intent["order_id"] else "ORDER_ALREADY_EXISTS", timing, intent=intent, error_code="NO_AUTOMATIC_RESUBMISSION")
            self._check_origin(event)
            if not isinstance(smoke_guard, RealOrderSmokeGuard):
                raise AutoGrabError("REAL_ORDER_SMOKE_TEST_NOT_ARMED")
            if smoke_guard.provider != intent["provider"]:
                raise AutoGrabError("SMOKE_PROVIDER_MISMATCH")
            if smoke_guard.status().get("REAL_ORDER_SMOKE_TEST_ARMED") is not True:
                raise AutoGrabError("REAL_ORDER_SMOKE_TEST_NOT_ARMED")
            check = await self.precheck_checkout(intent_id)
            if not isinstance(check, dict) or check.get("outcome") != "PRECHECK_READY":
                return self._result(event, "ORDER_PRECHECK", timing, intent=self.intents.get(intent_id), error_code="ORDER_PRECHECK_FAILED")
            preflight = await self.preflight()
            permit = smoke_guard.issue_permit(intent_id, check["precheck_id"], preflight)
            if not self.intents.begin_order_submission(intent_id, permit["nonce"], check["precheck_id"], submitted_at=permit["issued_at"]):
                return self._result(event, "ORDER_ALREADY_EXISTS", timing, intent=self.intents.get(intent_id), error_code="NO_AUTOMATIC_RESUBMISSION")
            timing.mark("T5")
            self._record(event, "ORDER_SUBMITTING", timing, ["ORDER_PRECHECK", "ORDER_SUBMITTING"], intent_id=intent_id)
            try:
                # A persisted marker is conservative: even a kill/expiry right
                # here becomes uncertain and never grants a replacement submit.
                smoke_guard.assert_permit(intent_id, permit, await self.preflight())
                self._check_origin(event)
                intent = self.intents.get(intent_id)
                receipt = await self.provider.submit_order(intent, product, permit)
                if not isinstance(receipt, dict) or receipt.get("status") not in {"FOUND", "ORDER_FOUND"}:
                    self.intents.mark_uncertain(intent_id)
                    return await self._reconcile_one(event, product, self.intents.get(intent_id), timing, [], recovery=False)
                result = await self._finish_receipt(event, product, intent, receipt, timing, [], state=PurchaseState.ORDER_SUBMITTING, recovery=False)
                self.intents.searching(intent_id)
                return result
            except asyncio.CancelledError:
                current = self.intents.get(intent_id)
                if not current["order_id"]:
                    self.intents.mark_uncertain(intent_id)
                raise
            except Exception as error:
                current = self.intents.get(intent_id)
                if not current["order_id"]:
                    current = self.intents.mark_uncertain(intent_id)
                self._record(event, current["state"], timing, [], intent_id=intent_id, error_code=_error_code(error))
                return self._result(event, current["state"], timing, intent=current, error_code=_error_code(error))
            finally:
                smoke_guard.disarm()

    async def process(self, supplied_event: dict[str, Any]) -> dict[str, Any]:
        async with self._worker:
            event = self.store.get_event(supplied_event.get("id"))
            if event is None:
                raise ValueError("Purchase requires an existing event")
            timing = PurchaseTiming()
            timing.mark("T0")
            existing = self.intents.get_by_event(event["id"]) or self.intents.active_for_product(event["product_id"], event["provider"])
            if existing is not None:
                status = _existing_status(event, existing)
                return self._result(event, status, timing, intent=existing,
                                    error_code=status if status != "ORDER_ALREADY_EXISTS" else None)
            event_rowid = self.store.connection.execute(
                "SELECT rowid FROM events WHERE id=?", (event["id"],)
            ).fetchone()[0]
            if event_rowid <= self._event_rowid_watermark:
                return self._result(event, "STALE_EVENT", timing, error_code="EVENT_PRECEDES_WORKER")
            if not self.store.claim_event(event["id"]):
                return self._result(event, "ALREADY_CLAIMED", timing)
            timeline: list[str] = []
            state = PurchaseState.INTENT_CREATED
            intent = None
            product = None

            def advance(target, *, verification=None, notification=None):
                nonlocal state
                state = transition(state, target, payment_verification=verification)
                timeline.append(state.value)
                self._record(event, state, timing, timeline,
                             intent_id=intent["intent_id"] if intent else None, notification=notification)

            try:
                if event["provider"] != "bandwagon" or event["event_type"] not in TRIGGER_TYPES:
                    raise AutoGrabError("EVENT_NOT_LIVE_ELIGIBLE")
                self._check_origin(event)
                snapshot = Product.from_dict(event["product"])
                if snapshot.eligible is not True or snapshot.availability != "AVAILABLE":
                    raise AutoGrabError("EVENT_NOT_LIVE_ELIGIBLE")
                # Fresh evidence and explicit ARM are prerequisites before any
                # provider cart operation, including configuration side effects.
                await self._check_guard()
                self._check_origin(event)
                intent = self.intents.create(event, snapshot)
                advance(PurchaseState.DETECTED)
                advance(PurchaseState.VERIFYING)
                product = await self.provider.check_product(event["product_id"])
                if not isinstance(product, Product) or (product.provider, product.product_id) != (event["provider"], event["product_id"]):
                    raise AutoGrabError("PRODUCT_MISMATCH")
                if product.availability != "AVAILABLE":
                    raise AutoGrabError("OUT_OF_STOCK" if product.availability == "SOLD_OUT" else "STOCK_UNKNOWN")
                if product.eligible is not True:
                    raise AutoGrabError("PRODUCT_NOT_PROMOTIONAL")
                timing.mark("T1")
                advance(PurchaseState.PRODUCT_VERIFIED)
                advance(PurchaseState.OPENING_BROWSER)
                self._check_origin(event)
                await self.provider.open_product(product)
                timing.mark("T2")
                # Lookup/navigation can take time; recheck immediately before
                # the first cart-side mutation rather than before that wait.
                await self._check_guard()
                self._check_origin(event)
                configuration = await self.provider.prepare_cart(product)
                self._check_origin(event)
                checkout = await self.provider.prepare_checkout(product, configuration)
                if not isinstance(checkout, dict) or checkout.get("checkout_verified") is not True:
                    raise AutoGrabError("CHECKOUT_UNVERIFIED")
                intent = self.intents.set_cart(intent["intent_id"], checkout.get("cart_identifier"))
                timing.mark("T3")
                advance(PurchaseState.CART_READY)
                intent = self.intents.mark_checkout_ready(intent["intent_id"])
                timing.mark("T4")
                advance(PurchaseState.CHECKOUT_READY)
                # Persist the single permission first, then re-read kill switch,
                # session, email, boundary and lease immediately before dispatch.
                if not self.intents.mark_submitting(intent["intent_id"]):
                    return self._result(event, "ORDER_ALREADY_EXISTS", timing, intent=self.intents.get(intent["intent_id"]))
                timing.mark("T5")
                advance(PurchaseState.ORDER_SUBMITTING)
                await self._check_guard()
                self._check_origin(event)
                intent = self.intents.get(intent["intent_id"])
                receipt = await self.provider.submit_unpaid_order(intent, product, checkout)
                return await self._finish_receipt(event, product, intent, receipt, timing, timeline,
                                                  state=state, recovery=False)
            except IntentConflict as error:
                existing = self.intents.get(error.intent_id)
                status = _existing_status(event, existing)
                code = status if status != "ORDER_ALREADY_EXISTS" else None
                self._record(event, status, timing, timeline, intent_id=error.intent_id, error_code=code)
                return self._result(event, status, timing, intent=existing, error_code=code)
            except asyncio.CancelledError:
                current = self.intents.get(intent["intent_id"]) if intent else None
                if current is not None and current["state"] == "ORDER_SUBMITTING":
                    current = self.intents.mark_uncertain(current["intent_id"])
                elif current is not None and current["submit_started_at"] is None and current["state"] in {"INTENT_CREATED", "CART_READY", "CHECKOUT_READY"}:
                    current = self.intents.abort_before_submit(current["intent_id"])
                self._record(event, current["state"] if current else "INTERRUPTED", timing, timeline,
                             intent_id=current["intent_id"] if current else None, error_code="INTERRUPTED")
                raise
            except Exception as error:
                code = _error_code(error)
                current = self.intents.get(intent["intent_id"]) if intent else None
                if current is not None and current["submit_started_at"] is not None:
                    if current["state"] == "ORDER_SUBMITTING":
                        current = self.intents.mark_uncertain(current["intent_id"])
                    # Never re-enter checkout or POST after a possible dispatch.
                    human_notice_sent = code in HUMAN_ERRORS
                    if human_notice_sent:
                        await self._notify_human(event, code)
                    return await self._reconcile_one(event, product or snapshot, current, timing, timeline,
                                                     recovery=False, original_error=code,
                                                     human_notice_sent=human_notice_sent)
                if current is not None:
                    current = self.intents.abort_before_submit(current["intent_id"])
                status = code if code in HUMAN_ERRORS else "LIVE_NOT_ARMED" if code in {
                    "DRY_RUN_CANNOT_SUBMIT", "LIVE_NOT_ARMED", "LIVE_PREFLIGHT_FAILED"} else "FAILED"
                self._record(event, status, timing, timeline, intent_id=current["intent_id"] if current else None, error_code=code)
                await self._notify_human(event, code)
                return self._result(event, status, timing, intent=current, error_code=code)

    async def _finish_receipt(self, event, product, intent, receipt, timing, timeline, *, state, recovery):
        if not isinstance(receipt, dict):
            raise AutoGrabError("ORDER_RECEIPT_UNVERIFIED")
        # Only the allowlisted receipt fields reach persistent storage; account
        # and hidden form data returned by an adapter are not retained or logged.
        fields = {name: receipt.get(name) for name in ("order_id", "invoice_id", "payment_url", "verification")}
        intent = self.intents.persist_result(intent["intent_id"], **fields)

        def advance(target, *, verification=None, notification=None):
            nonlocal state
            state = transition(state, target, payment_verification=verification)
            timeline.append(state.value)
            self._record(event, state, timing, timeline, intent_id=intent["intent_id"], notification=notification)

        if state == PurchaseState.RECONCILIATION_REQUIRED:
            advance(PurchaseState.ORDER_ALREADY_EXISTS)
        if not recovery and "T6" not in timing.marks:
            timing.mark("T6")
        advance(PurchaseState.ORDER_CREATED)
        if intent["invoice_id"] is None:
            if intent.get('submission_nonce'):
                intent = self.intents.searching(intent['intent_id'])
                advance(PurchaseState.INVOICE_SEARCHING)
            else:
                advance(PurchaseState.INVOICE_NOT_FOUND)
            return self._result(event, state, timing, intent=intent, error_code="INVOICE_NOT_FOUND", recovery=recovery)
        advance(PurchaseState.INVOICE_CREATED)
        if intent["payment_url"] is None:
            if intent.get('submission_nonce'):
                intent = self.intents.searching(intent['intent_id'])
                advance(PurchaseState.PAYMENT_LINK_SEARCHING)
            else:
                advance(PurchaseState.PAYMENT_URL_NOT_FOUND)
            return self._result(event, state, timing, intent=intent, error_code="PAYMENT_URL_NOT_FOUND", recovery=recovery)
        timing.mark("T7")
        advance(PurchaseState.PAYMENT_URL_READY)
        # Recovery may retain a previously verified historical receipt. That
        # does not establish that the page is still unpaid/actionable now.
        # Only complete matching evidence from THIS adapter read can notify.
        if intent["payment_page_verified"] is not True or not isinstance(receipt.get("verification"), dict):
            return self._result(event, state, timing, intent=intent, error_code="PAYMENT_PAGE_UNVERIFIED", recovery=recovery)
        timing.mark("T8")
        advance(PurchaseState.PAYMENT_READY, verification=intent["verification"])
        intent, notification = await self._notify_payment(event, product, intent, timing)
        advance(PurchaseState.WAITING_FOR_USER, notification=notification)
        return self._result(event, state, timing, intent=intent, notification=notification, recovery=recovery)

    async def _reconcile_one(self, event, product, intent, timing, timeline, *, recovery, original_error=None,
                             human_notice_sent=False):
        try:
            self._check_origin(event)
            if intent.get("submission_nonce"):
                intent = self.intents.searching(intent["intent_id"])
            receipt = await self.provider.reconcile_intent(intent, product)
            if isinstance(receipt, dict) and receipt.get("status") in {"LOGIN_REQUIRED", "HUMAN_ACTION_REQUIRED"}:
                raise AutoGrabError("LOGIN_REQUIRED" if receipt["status"] == "LOGIN_REQUIRED" else "CAPTCHA_REQUIRED")
            if not isinstance(receipt, dict) or receipt.get("status") not in {"FOUND", "ORDER_FOUND", "ABSENT", "NO_ORDER_FOUND", "UNKNOWN"}:
                raise AutoGrabError("RECONCILIATION_UNVERIFIED")
            status = {"ORDER_FOUND": "FOUND", "NO_ORDER_FOUND": "ABSENT"}.get(receipt["status"], receipt["status"])
            fields = {name: receipt.get(name) for name in ("order_id", "invoice_id", "payment_url", "verification")}
            reconciled = self.intents.reconcile(intent["intent_id"], status, **fields, absence_evidence=receipt.get("absence_evidence"))
            if status == "FOUND":
                return await self._finish_receipt(event, product, reconciled, receipt, timing, timeline,
                                                  state=PurchaseState.RECONCILIATION_REQUIRED, recovery=recovery)
            code = "ORDER_ABSENCE_CONFIRMED_NO_AUTORETRY" if status == "ABSENT" else "RECONCILIATION_REQUIRED"
            current_status = _unverified_status(reconciled)
            self._record(event, current_status, timing, timeline, intent_id=intent["intent_id"], error_code=code)
            return self._result(event, current_status, timing, intent=reconciled, error_code=code, recovery=recovery)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = _error_code(error)
            current = self.intents.get(intent["intent_id"])
            if current["state"] == "ORDER_SUBMITTING":
                current = self.intents.mark_uncertain(intent["intent_id"])
            # Preserve known receipt fields; an unavailable account page is not
            # evidence of absence and must not release the active-product lock.
            current_status = _unverified_status(current)
            self._record(event, current_status, timing, timeline, intent_id=intent["intent_id"],
                         error_code=code if code in HUMAN_ERRORS else original_error or code)
            if not human_notice_sent:
                await self._notify_human(event, code)
            return self._result(event, current_status, timing, intent=current,
                                error_code=code if code in HUMAN_ERRORS else original_error or code, recovery=recovery)

    async def recover(self) -> list[dict[str, Any]]:
        """Read server state while disarmed; never create a replacement order."""
        async with self._worker:
            self.intents.recover_interrupted()
            pending = {item["intent_id"]: item for item in self.intents.recovery_intents()}
            # Crash between durable PAYMENT_READY and notification is recoverable.
            pending.update({item["intent_id"]: item for item in self.intents.list(active_only=True)
                            if item["state"] == "PAYMENT_READY"})
            results = []
            for intent in pending.values():
                if intent['provider'] != getattr(self.provider, 'provider_name', 'bandwagon'):
                    continue
                event = self.store.get_event(intent["event_id"])
                if event is None:
                    continue  # Foreign key normally makes this impossible.
                if event["simulated"] and not self._simulation_allowed():
                    continue
                product = Product.from_dict(event["product"])
                timing = PurchaseTiming()  # No fabricated detection timestamp.
                results.append(await self._reconcile_one(event, product, intent, timing, [], recovery=True))
            return results
