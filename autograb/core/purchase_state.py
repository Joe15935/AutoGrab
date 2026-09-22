"""Order preparation and recovery states. There is deliberately no payment action."""

from enum import StrEnum
from urllib.parse import parse_qs, urlsplit

from .errors import AutoGrabError


class PurchaseState(StrEnum):
    INTENT_CREATED = "INTENT_CREATED"
    DETECTED = "DETECTED"
    VERIFYING = "VERIFYING"
    PRODUCT_VERIFIED = "PRODUCT_VERIFIED"
    OPENING_BROWSER = "OPENING_BROWSER"
    CART_READY = "CART_READY"
    CHECKOUT_READY = "CHECKOUT_READY"
    ORDER_SUBMITTING = "ORDER_SUBMITTING"
    ORDER_CREATED = "ORDER_CREATED"
    INVOICE_CREATED = "INVOICE_CREATED"
    PAYMENT_URL_READY = "PAYMENT_URL_READY"
    PAYMENT_READY = "PAYMENT_READY"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_ALREADY_EXISTS = "ORDER_ALREADY_EXISTS"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ORDER_SUBMIT_FAILED = "ORDER_SUBMIT_FAILED"
    INVOICE_NOT_FOUND = "INVOICE_NOT_FOUND"
    PAYMENT_URL_NOT_FOUND = "PAYMENT_URL_NOT_FOUND"
    PAYMENT_GATEWAY_SELECTION_REQUIRED = "PAYMENT_GATEWAY_SELECTION_REQUIRED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PROVIDER_CHANGED = "PROVIDER_CHANGED"
    LIVE_NOT_ARMED = "LIVE_NOT_ARMED"
    FAILED = "FAILED"


EDGES = {
    PurchaseState.INTENT_CREATED: {PurchaseState.DETECTED},
    PurchaseState.DETECTED: {PurchaseState.VERIFYING},
    PurchaseState.VERIFYING: {PurchaseState.PRODUCT_VERIFIED},
    PurchaseState.PRODUCT_VERIFIED: {PurchaseState.OPENING_BROWSER},
    PurchaseState.OPENING_BROWSER: {PurchaseState.CART_READY},
    PurchaseState.CART_READY: {PurchaseState.CHECKOUT_READY},
    PurchaseState.CHECKOUT_READY: {PurchaseState.ORDER_SUBMITTING, PurchaseState.ORDER_ALREADY_EXISTS},
    PurchaseState.ORDER_SUBMITTING: {
        PurchaseState.ORDER_CREATED, PurchaseState.RECONCILIATION_REQUIRED, PurchaseState.ORDER_SUBMIT_FAILED,
    },
    PurchaseState.RECONCILIATION_REQUIRED: {PurchaseState.ORDER_ALREADY_EXISTS, PurchaseState.ORDER_SUBMIT_FAILED},
    PurchaseState.ORDER_ALREADY_EXISTS: {PurchaseState.ORDER_CREATED, PurchaseState.INVOICE_NOT_FOUND},
    PurchaseState.ORDER_CREATED: {PurchaseState.INVOICE_CREATED, PurchaseState.INVOICE_NOT_FOUND},
    PurchaseState.INVOICE_NOT_FOUND: {PurchaseState.RECONCILIATION_REQUIRED},
    PurchaseState.INVOICE_CREATED: {PurchaseState.PAYMENT_URL_READY, PurchaseState.PAYMENT_URL_NOT_FOUND},
    PurchaseState.PAYMENT_URL_NOT_FOUND: {PurchaseState.RECONCILIATION_REQUIRED},
    PurchaseState.PAYMENT_URL_READY: {PurchaseState.PAYMENT_READY, PurchaseState.PAYMENT_GATEWAY_SELECTION_REQUIRED},
    PurchaseState.PAYMENT_READY: {PurchaseState.WAITING_FOR_USER, PurchaseState.ORDER_EXPIRED, PurchaseState.ORDER_CANCELLED},
    PurchaseState.WAITING_FOR_USER: {PurchaseState.ORDER_EXPIRED, PurchaseState.ORDER_CANCELLED},
    PurchaseState.PAYMENT_GATEWAY_SELECTION_REQUIRED: {PurchaseState.RECONCILIATION_REQUIRED},
}

PRE_SUBMIT = {
    PurchaseState.INTENT_CREATED, PurchaseState.DETECTED, PurchaseState.VERIFYING,
    PurchaseState.PRODUCT_VERIFIED, PurchaseState.OPENING_BROWSER,
    PurchaseState.CART_READY, PurchaseState.CHECKOUT_READY,
}
HUMAN_BLOCKS = {
    PurchaseState.LOGIN_REQUIRED, PurchaseState.CAPTCHA_REQUIRED, PurchaseState.SESSION_EXPIRED,
    PurchaseState.PROVIDER_CHANGED,
}
TERMINAL = {
    PurchaseState.ORDER_EXPIRED, PurchaseState.ORDER_CANCELLED, PurchaseState.ORDER_SUBMIT_FAILED,
    PurchaseState.LIVE_NOT_ARMED, PurchaseState.FAILED,
}


def assert_payment_ready(evidence: dict | None) -> None:
    """Require page-verification evidence; an invoice-looking URL is insufficient.

    The provider must inspect the current page and compare merchant, intent,
    product and amount. Only an official invoice URL is used in this first
    implementation; gateway navigation is unnecessary to hand payment to a user.
    """
    if not isinstance(evidence, dict):
        raise AutoGrabError("PAYMENT_PAGE_UNVERIFIED")
    for name in ("order_id", "invoice_id"):
        identifier = evidence.get(name)
        if (not isinstance(identifier, str) or not identifier.isascii()
                or not identifier.isdecimal() or not 1 <= len(identifier) <= 64):
            raise AutoGrabError("PAYMENT_PAGE_UNVERIFIED")
    if any(evidence.get(name) is not True for name in (
        "merchant_verified", "product_verified", "amount_present", "payment_page_verified", "unpaid_verified",
    )):
        raise AutoGrabError("PAYMENT_PAGE_UNVERIFIED")
    try:
        parsed = urlsplit(evidence.get("payment_url", ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid = (
            parsed.scheme == "https" and parsed.netloc == "bandwagonhost.com"
            and parsed.path == "/viewinvoice.php" and not parsed.fragment
            and query == {"id": [evidence["invoice_id"]]}
            and evidence["payment_url"] == f"https://bandwagonhost.com/viewinvoice.php?id={evidence['invoice_id']}"
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise AutoGrabError("PAYMENT_PAGE_UNVERIFIED")


def transition(current: PurchaseState | str, target: PurchaseState | str,
               *, payment_verification: dict | None = None) -> PurchaseState:
    current, target = PurchaseState(current), PurchaseState(target)
    if current in TERMINAL:
        raise ValueError(f"Invalid purchase transition: {current} -> {target}")
    if target in HUMAN_BLOCKS and current not in HUMAN_BLOCKS:
        return target
    if current in HUMAN_BLOCKS and target == PurchaseState.RECONCILIATION_REQUIRED:
        return target
    if current in PRE_SUBMIT and target in {PurchaseState.FAILED, PurchaseState.LIVE_NOT_ARMED}:
        return target
    if target not in EDGES.get(current, set()):
        raise ValueError(f"Invalid purchase transition: {current} -> {target}")
    if target == PurchaseState.PAYMENT_READY:
        assert_payment_ready(payment_verification)
    return target
