"""TLS-only SMTP notifications with explicit, non-file credential sources.

SMTP acceptance is a transport result, not proof of inbox delivery. There is no
automatic retry here: a connection lost after DATA can have an uncertain result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import math
import os
import re
import smtplib
import ssl
import sys
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from uuid import uuid4

from autograb.core.models import Product


KEYCHAIN_SERVICE = "AutoGrab SMTP"
PROVIDER_LABELS = {"bandwagon": "BandwagonHost", "dmit": "DMIT", "vmiss": "VMISS", "vps": "V.PS", "apple": "Apple"}


def safe_provider_url(product):
    if product.provider == "bandwagon":
        return safe_public_url(product.product_url)
    try:
        from autograb.edge.protocol import public_url
        return public_url(product.product_url, provider=product.provider, product=True)
    except (ValueError, TypeError):
        return "UNAVAILABLE"


def _text(value: Any, limit: int = 300) -> str:
    """Only format selected scalar fields, never arbitrary event dictionaries."""
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return "UNKNOWN"
    return " ".join(str(value).split())[:limit] or "UNKNOWN"


def _plain(value: str) -> bool:
    return not any(ord(character) < 32 or ord(character) == 127 for character in value)


def _address_valid(value: str) -> bool:
    if not _plain(value) or len(value) > 254:
        return False
    try:
        address = Address(addr_spec=value)
        return bool(address.username and address.domain and address.addr_spec == value)
    except (ValueError, IndexError):
        return False


@dataclass(frozen=True)
class SMTPConfig:
    host: str = ""
    port: int = 465
    username: str = field(default="", repr=False)
    sender: str = field(default="", repr=False)
    recipient: str = field(default="", repr=False)
    tls_mode: str = "ssl"
    secret_service: str = KEYCHAIN_SERVICE
    secret_account: str = field(default="", repr=False)
    timeout: float = 10.0
    _password: str = field(default="", repr=False, compare=False)
    _parse_error: bool = field(default=False, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SMTPConfig":
        return cls.from_mapping({}, env)

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, Any] | None, env: Mapping[str, str] | None = None
    ) -> "SMTPConfig":
        """Read non-secret config plus temporary environment overrides.

        Accept either the SMTP section or a top-level ``smtp`` section. Password
        fields in a config mapping are rejected. No .env file is loaded or saved.
        Keychain is accessed only when sending with an explicit secret_account.
        """
        source = values or {}
        if isinstance(source.get("smtp"), Mapping):
            source = source["smtp"]
        environment = os.environ if env is None else env

        def read(key: str, environment_key: str, default: Any = "", alias: str | None = None) -> Any:
            fallback = environment.get(alias, source.get(key, default)) if alias else source.get(key, default)
            autograb_alias = {"host": "AUTOGRAB_SMTP_HOST", "username": "AUTOGRAB_SMTP_USER",
                              "recipient": "AUTOGRAB_EMAIL_TO"}.get(key)
            if autograb_alias:
                fallback = environment.get(autograb_alias, fallback)
            return environment.get(environment_key, fallback)

        mode = str(read("tls_mode", "SMTP_TLS_MODE", "ssl")).lower()
        invalid = any(key.lower() in {"password", "smtp_password", "_password"} for key in source)
        try:
            port = int(read("port", "SMTP_PORT", 587 if mode == "starttls" else 465))
            timeout = float(read("timeout", "SMTP_TIMEOUT", 10))
        except (ValueError, TypeError, OverflowError):
            port, timeout, invalid = 0, 10.0, True
        return cls(
            host=str(read("host", "SMTP_HOST")),
            port=port,
            username=str(read("username", "SMTP_USERNAME")),
            sender=str(read("sender", "SMTP_SENDER", environment.get("AUTOGRAB_SMTP_USER", ""), alias="EMAIL_FROM")),
            recipient=str(read("recipient", "SMTP_RECIPIENT", alias="EMAIL_TO")),
            tls_mode=mode,
            secret_service=str(read("secret_service", "SMTP_KEYCHAIN_SERVICE", KEYCHAIN_SERVICE)),
            secret_account=str(read("secret_account", "SMTP_KEYCHAIN_ACCOUNT")),
            timeout=timeout,
            _password=environment.get("SMTP_PASSWORD", environment.get("AUTOGRAB_SMTP_PASSWORD", "")),
            _parse_error=invalid,
        )

    @property
    def error_code(self) -> str | None:
        if self._parse_error:
            return "CONFIG_INVALID"
        if not all((self.host, self.username, self.sender, self.recipient)):
            return "NOT_CONFIGURED"
        if (
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", self.host)
            or not _plain(self.username)
            or not _address_valid(self.sender)
            or not _address_valid(self.recipient)
            or (self.tls_mode, self.port) not in {("ssl", 465), ("starttls", 587)}
            or not math.isfinite(self.timeout)
            or not 1 <= self.timeout <= 60
            or self.secret_service != KEYCHAIN_SERVICE
            or not _plain(self.secret_account)
        ):
            return "CONFIG_INVALID"
        if not (self._password or self.secret_account):
            return "NOT_CONFIGURED"
        return None


@dataclass(frozen=True)
class NotificationResult:
    status: str
    error_code: str | None = None
    detail: str = ""


class _CredentialError(Exception):
    pass


def safe_public_url(value: Any) -> str:
    """Allow only public Bandwagon catalog or read-only cart-view URLs.

    All fragments and unrecognized query parameters are removed. Purchase,
    session, payment and arbitrary paths are never included in email links.
    Cart/configuration links remain tied to the original browser session.
    """
    if not isinstance(value, str) or not _plain(value) or len(value) > 2048:
        return "UNAVAILABLE"
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.hostname not in {"bandwagonhost.com", "www.bandwagonhost.com"}
            or url.username is not None
            or url.password is not None
            or url.port not in {None, 443}
        ):
            return "UNAVAILABLE"
        path = unquote(url.path)
        if not _plain(path) or ".." in path.split("/") or "%" in path or "\\" in path:
            return "UNAVAILABLE"
        if path == "/cart.php":
            if parse_qs(url.query).get("a") not in (["view"], ["confproduct"]):
                return "UNAVAILABLE"
            return urlunsplit(("https", url.hostname, "/cart.php", "a=view", ""))
        public_tier = re.fullmatch(r"/order/(?:basic|ultra|ecommerce|ecommerce-sla-elevated)(?:/[^/?#]+/[^/?#]+)?/?", path)
        if path not in {"", "/", "/index.php", "/order"} and not public_tier:
            return "UNAVAILABLE"
        return urlunsplit(("https", url.hostname, url.path or "/", "", ""))
    except (ValueError, TypeError):
        return "UNAVAILABLE"


def _stage(timing: Mapping[str, Any], name: str, mark: str) -> str:
    for collection in (timing, timing.get("marks", {}), timing.get("stages", {})):
        if not isinstance(collection, Mapping):
            continue
        value = collection.get(name, collection.get(mark))
        if isinstance(value, Mapping):
            value = value.get("utc", value.get("wall_time", value.get("timestamp")))
        if value is not None:
            return _text(value)
    return "NOT REACHED"


def safe_invoice_url(value: Any, invoice_id: Any, provider: str = "bandwagon") -> str:
    """Allow the exact official invoice reference, without bearer/session data.

    This validates a link's shape only. The caller must separately verify the
    actual page, order, product, amount and unpaid status before notifying.
    """
    identifier = str(invoice_id) if isinstance(invoice_id, (str, int)) and not isinstance(invoice_id, bool) else ""
    hosts = {"bandwagon": {"bandwagonhost.com", "www.bandwagonhost.com"}, "dmit": {"www.dmit.io"}}
    if provider not in hosts or not re.fullmatch(r"[1-9][0-9]{0,39}", identifier):
        return "UNAVAILABLE"
    if not isinstance(value, str) or not _plain(value) or len(value) > 2048:
        return "UNAVAILABLE"
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.netloc not in hosts[provider]
                or url.path != "/viewinvoice.php" or url.fragment
                or url.query != f"id={identifier}"):
            return "UNAVAILABLE"
        return value
    except (ValueError, TypeError):
        return "UNAVAILABLE"


def _payment_total(timing: Mapping[str, Any]) -> str:
    for collection in (timing, timing.get("durations_ms", {})):
        if not isinstance(collection, Mapping):
            continue
        for key in ("detection_to_payment_ready_ms", "detection_to_payment_ready"):
            value = collection.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                return f"{value / 1000:.3f} sec"
    return "UNKNOWN"


def _payment_timestamp(timing: Mapping[str, Any], name: str, mark: str, *fallbacks: Any) -> str:
    """Use recorded timezone-aware timestamps, never infer order completion."""
    for value in (_stage(timing, name, mark), *fallbacks):
        if not isinstance(value, str):
            continue
        try:
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is not None and stamp.utcoffset() is not None:
                return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        except ValueError:
            pass
    return "UNKNOWN"


def _payment_elapsed(timing: Mapping[str, Any], detected: str, ready: str) -> str:
    duration = _payment_total(timing)
    if duration != "UNKNOWN":
        return duration
    try:
        seconds = (datetime.fromisoformat(ready) - datetime.fromisoformat(detected)).total_seconds()
        if seconds >= 0:
            return f"{seconds:.3f} sec (wall-clock)"
    except ValueError:
        pass
    return "UNKNOWN"


def _total(timing: Mapping[str, Any]) -> str:
    keys = ("detection_to_boundary_ms", "Detection → Boundary", "detection_to_boundary")
    for collection in (timing, timing.get("durations_ms", {}), timing.get("elapsed_ms", {})):
        if not isinstance(collection, Mapping):
            continue
        for key in keys:
            value = collection.get(key)
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                return f"{value / 1000:.3f} sec"
    return "UNKNOWN"


def _prices(product: Product) -> tuple[str, str]:
    prices, cycles = [], []
    for row in product.prices[:12]:
        if not isinstance(row, Mapping):
            continue
        amount = row.get("price", row.get("amount", row.get("value")))
        if "cents" in row:
            cents = row["cents"]
            if isinstance(cents, int) and not isinstance(cents, bool):
                amount = f"{cents // 100}.{cents % 100:02d}" if cents >= 0 else "UNAVAILABLE"
            else:
                amount = None
        currency = row.get("currency")
        prices.append(" ".join(filter(None, (_text(currency) if currency is not None else "", _text(amount)))))
        cycles.append(_text(row.get("billing_cycle", row.get("billingcycle", row.get("cycle", row.get("period"))))))
    return "; ".join(prices) or "UNKNOWN", "; ".join(cycles) or "UNKNOWN"


class EmailNotifier:
    def __init__(self, config: SMTPConfig):
        self.config = config

    @property
    def configured(self) -> bool:
        """Configuration completeness only; never queries Keychain or SMTP."""
        return self.config.error_code is None

    def _configuration_result(self) -> NotificationResult | None:
        error = self.config.error_code
        if error == "NOT_CONFIGURED":
            return NotificationResult("NOT_CONFIGURED", error, "SMTP settings or credential source are incomplete.")
        if error:
            return NotificationResult("NOTIFICATION_FAILED", error, "SMTP configuration is invalid.")
        return None

    def _message(self, subject: str, body: str, event_id: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.sender
        message["To"] = self.config.recipient
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        message["Message-ID"] = f"<autograb.{digest}@autograb.local>"
        message.set_content(body)
        return message

    async def send_test(self) -> NotificationResult:
        if result := self._configuration_result():
            return result
        message = self._message(
            "AutoGrab Email Test",
            "AutoGrab Email Test\n\nProvider:\nBandwagonHost\n\nMode:\nDRY RUN\n\n"
            "Status:\nEMAIL SYSTEM WORKING\n\n"
            "SMTP acceptance does not confirm inbox delivery.\n",
            f"test:{uuid4()}",
        )
        return await asyncio.to_thread(self._send, message)

    async def send_live_test(self) -> NotificationResult:
        """Explicit test used by configuration and the LIVE pre-flight gate."""
        if result := self._configuration_result():
            return result
        message = self._message(
            "[AUTOGRAB TEST] Email notification working",
            "AutoGrab\n\nEmail notification transport test.\n\n"
            "If SMTP accepts this message, connection, TLS, authentication and the delivery request succeeded.\n"
            "SMTP ACCEPTED does not confirm DELIVERED TO INBOX.\n"
            "This test creates no merchant order and submits no payment.\n",
            f"live-test:{uuid4()}",
        )
        return await asyncio.to_thread(self._send, message)

    async def send_payment_ready(
        self, product: Product, event: Mapping[str, Any], intent: Mapping[str, Any], timing: Mapping[str, Any]
    ) -> NotificationResult:
        """Notify only a persisted, independently page-verified unpaid invoice."""
        if result := self._configuration_result():
            return result
        try:
            from autograb.core.errors import AutoGrabError
            from autograb.core.purchase_state import assert_payment_ready
            identity = intent.get("intent_id", intent.get("id"))
            provider = product.provider
            merchant = {"bandwagon": "bandwagonhost.com", "dmit": "www.dmit.io"}.get(provider)
            invoice_id = intent.get("invoice_id")
            order_id = intent.get("order_id")
            payment_url = safe_invoice_url(intent.get("payment_url"), invoice_id, provider)
            evidence = intent.get("verification", {})
            if (not isinstance(evidence, Mapping) or not identity
                    or intent.get("state", intent.get("status")) not in {"PAYMENT_READY", "WAITING_FOR_USER"}
                    or intent.get("origin") != "REAL"
                    or merchant is None or intent.get("provider") != provider
                    or event.get("provider", provider) != provider or event.get("simulated", False) is not False
                    or str(intent.get("product_id")) != product.product_id
                    or not isinstance(order_id, (str, int)) or isinstance(order_id, bool)
                    or not re.fullmatch(r"[1-9][0-9]{0,39}", str(order_id))
                    or evidence.get("source") != "REAL_SITE"
                    or evidence.get("merchant") != merchant or evidence.get("provider", provider) != provider
                    or ("login_required" in evidence and type(evidence["login_required"]) is not bool)
                    or str(evidence.get("product_id")) != product.product_id
                    or str(evidence.get("order_id")) != str(order_id)
                    or str(evidence.get("invoice_id")) != str(invoice_id)
                    or evidence.get("payment_url") != payment_url
                    or any(evidence.get(flag) is not True for flag in (
                        "merchant_verified", "product_verified", "amount_present", "payment_page_verified", "unpaid_verified"))
                    or str(evidence.get("invoice_status", "")).casefold() != "unpaid"
                    or payment_url == "UNAVAILABLE"):
                return NotificationResult("NOTIFICATION_FAILED", "PAYMENT_NOT_VERIFIED", "An independently verified unpaid invoice is required.")
            try:
                assert_payment_ready({**evidence, "provider": provider})
            except AutoGrabError:
                return NotificationResult("NOTIFICATION_FAILED", "PAYMENT_NOT_VERIFIED", "An independently verified unpaid invoice is required.")
            price, billing = _prices(product)
            price = _text(evidence.get("amount", intent.get("amount"))) if evidence.get("amount", intent.get("amount")) is not None else price
            cents, currency = evidence.get("amount_cents"), evidence.get("currency")
            if type(cents) is int and 0 < cents < 10**12 and currency in {"USD", "EUR", "CNY", "HKD", "CAD", "GBP"}:
                price = f"{currency} {cents // 100}.{cents % 100:02d}"
            billing = _text(evidence.get("billing", intent.get("billing"))) if evidence.get("billing", intent.get("billing")) is not None else billing
            detected = _payment_timestamp(timing, "detection", "T0", event.get("detected_at"), event.get("created_at"))
            created = _payment_timestamp(timing, "order_created", "T6", intent.get("order_created_at"))
            ready = _payment_timestamp(timing, "payment_ready", "T8", intent.get("payment_ready_at"))
            label = PROVIDER_LABELS[provider]
            fields = (
                ("Provider", label),
                ("Event", _text(event.get("event_type", event.get("type")))),
                ("Product", _text(product.name)),
                ("Product ID", _text(product.product_id)),
                ("Price", price), ("Billing", billing),
                ("Order ID", str(order_id)), ("Invoice ID", str(invoice_id)),
                ("Status", "PAYMENT_READY"),
                ("Detected", detected),
                ("Order created", created),
                ("Payment ready", ready),
                ("Email", datetime.now(timezone.utc).isoformat(timespec="milliseconds") + " (message prepared; SMTP acceptance is recorded locally afterward)"),
                ("Detection → Payment Ready", _payment_elapsed(timing, detected, ready)),
                ("Payment deadline", _text(intent.get("payment_deadline", evidence.get("deadline")))),
                ("Official payment URL", payment_url),
            )
            body = "AutoGrab\n\n" + "\n\n".join(f"{label}:\n{value}" for label, value in fields)
            login = "Login required." if evidence.get("login_required") is True else "Login may be required on this phone or browser."
            body += (f"\n\n{login} Use your own {label} account to open the official invoice HTTPS link above.\n"
                     "Order created and Payment ready are AutoGrab observation times, not a server processing-time guarantee.\n"
                     "AutoGrab has not submitted payment. You decide whether to pay.\n"
                     "An unpaid invoice does not establish inventory reservation.\n"
                     "SMTP acceptance does not confirm inbox delivery.\n")
            message = self._message("🚨🚨 [PAY NOW] AutoGrab Payment Ready", body, f"payment-ready:{provider}:{identity}")
        except Exception:
            return NotificationResult("NOTIFICATION_FAILED", "INVALID_MESSAGE", "Notification content could not be prepared.")
        return await asyncio.to_thread(self._send, message)

    async def send_session_required(self, notice_id: str, status: str = "LOGIN_REQUIRED", *, provider: str = 'bandwagon') -> NotificationResult:
        """Only explicit status strings enter a notice; no private page content."""
        if result := self._configuration_result():
            return result
        if provider not in {'bandwagon', 'dmit'} or status not in {"LOGIN_REQUIRED", "SESSION_EXPIRED", "CAPTCHA_REQUIRED"} or not isinstance(notice_id, str) or not notice_id:
            return NotificationResult("NOTIFICATION_FAILED", "INVALID_NOTICE", "An explicit session notice is required.")
        label = PROVIDER_LABELS[provider]
        body = (f"AutoGrab\n\nProvider:\n{label}\n\nStatus:\n{status}\n\n"
                "USER ACTION REQUIRED: Open the original normal Edge tab and complete login or verification yourself.\n"
                "New order creation is blocked. No automatic payment is performed.\n"
                "No password or verification code should be sent by email or chat.\n")
        message = self._message(f"[AUTOGRAB] {label} 需要本人登录或验证", body, f"session:{provider}:{notice_id}:{status}")
        return await asyncio.to_thread(self._send, message)

    async def send_edge_human_required(self, notice_id: str, status: str) -> NotificationResult:
        """Edge companion pause; the caller claims one durable notice before send."""
        if result := self._configuration_result():
            return result
        if (status not in {"LOGIN_REQUIRED", "HUMAN_CHALLENGE_REQUIRED"}
                or not isinstance(notice_id, str)
                or not re.fullmatch(r"edge:[a-f0-9-]{36}:[0-9]{1,9}", notice_id)):
            return NotificationResult("NOTIFICATION_FAILED", "INVALID_NOTICE")
        body = (f"AutoGrab Edge Companion\n\n当前商家\n\nStatus: {status}\n\n"
                "AutoGrab 已暂停，请在当前 Microsoft Edge 窗口中本人完成登录或真人验证。\n"
                "验证完成后，使用同一个 AutoGrab intent 继续；不要重复创建购买任务。\n"
                "订单结果以已保存的状态和商家核实结果为准；自动付款保持关闭。\n"
                "不要在聊天或邮件中提供密码、验证码或浏览器会话数据。\n")
        message = self._message("[AUTOGRAB] Edge 需要本人登录或验证", body, notice_id)
        return await asyncio.to_thread(self._send, message)

    async def send_event(
        self, product: Product, event: dict, timing: dict, boundary: dict
    ) -> NotificationResult:
        if result := self._configuration_result():
            return result
        try:
            event_id = event.get("event_id", event.get("id"))
            if event_id is None or str(event_id) == "":
                return NotificationResult("NOTIFICATION_FAILED", "INVALID_EVENT", "A persisted event ID is required.")
            price, billing = _prices(product)
            label = PROVIDER_LABELS.get(product.provider, "UNKNOWN")
            fields = (
                ("Provider", label),
                ("Mode", "DRY RUN"),
                ("Event", _text(event.get("event_type", event.get("type", event.get("event"))))),
                ("Product", _text(product.name)),
                ("Product ID", _text(product.product_id)),
                ("Price", price),
                ("Billing", billing),
                ("Status", _text(boundary.get("status"))),
                ("Detected", _stage(timing, "detection", "T0")),
                ("Cart Ready", _stage(timing, "cart_ready", "T3")),
                ("Dry Run Boundary", _stage(timing, "boundary", "T4")),
                ("Total", _total(timing)),
                ("Product URL", safe_provider_url(product)),
                ("Cart URL", safe_public_url(boundary.get("cart_url"))),
            )
            if product.provider == "apple":
                inventory = product.metadata.get("inventory", [])
                rows = [" | ".join(_text(item.get(key)) for key in
                    ("sku", "region", "mode", "store_id", "availability")) +
                    (" | fresh" if item.get("fresh") is True else " | unverified/stale")
                    for item in inventory if isinstance(item, dict)][:80]
                opportunities = event.get("details", {}).get("opportunities", [])
                fields += (("Observed opportunity", ", ".join(_text(v) for v in opportunities) or "RESEARCH / REGULAR"),
                           ("Apple inventory (SKU | region | mode | store | state | freshness)", "\n".join(rows) or "UNKNOWN"))
            body = "\n\n".join(f"{label}:\n{value}" for label, value in fields)
            body += "\n\nNo order or payment was created by AutoGrab. Cart access is session-bound and may require the original browser session. Cart/configuration visibility does not reserve inventory.\n"
            message = self._message(
                f"🧪 [AUTOGRAB DRY RUN] {label} Opportunity Detected",
                body,
                f"{product.provider}:{event_id}",
            )
        except Exception:
            return NotificationResult("NOTIFICATION_FAILED", "INVALID_MESSAGE", "Notification content could not be prepared.")
        return await asyncio.to_thread(self._send, message)

    def _password(self) -> str:
        if self.config._password:
            return self.config._password
        if not self.config.secret_account or sys.platform != "darwin":
            raise _CredentialError("CREDENTIAL_UNAVAILABLE")
        try:
            import keyring
            from keyring.backends.macOS import Keyring

            backend = keyring.get_keyring()
            if type(backend) is not Keyring:
                raise _CredentialError("UNSAFE_KEYRING_BACKEND")
            password = backend.get_password(KEYCHAIN_SERVICE, self.config.secret_account)
            if not password:
                raise _CredentialError("CREDENTIAL_UNAVAILABLE")
            return password
        except _CredentialError:
            raise
        except Exception:
            raise _CredentialError("CREDENTIAL_UNAVAILABLE") from None

    def _send(self, message: EmailMessage) -> NotificationResult:
        client = None
        dispatched = False
        try:
            password = self._password()
            context = ssl.create_default_context()
            if self.config.tls_mode == "ssl":
                client = smtplib.SMTP_SSL(self.config.host, self.config.port, timeout=self.config.timeout, context=context)
            else:
                client = smtplib.SMTP(self.config.host, self.config.port, timeout=self.config.timeout)
                if client.ehlo()[0] != 250:
                    raise smtplib.SMTPHeloError(0, b"EHLO failed")
                client.starttls(context=context)
            if client.ehlo()[0] != 250:
                raise smtplib.SMTPHeloError(0, b"EHLO failed")
            client.login(self.config.username, password)
            dispatched = True
            refused = client.send_message(message, from_addr=self.config.sender, to_addrs=[self.config.recipient])
            if refused:
                return NotificationResult("NOTIFICATION_FAILED", "SMTP_RECIPIENT_REJECTED", "SMTP rejected the recipient.")
            return NotificationResult("SMTP_ACCEPTED", detail="SMTP server accepted the message; inbox delivery is unverified.")
        except _CredentialError as error:
            return NotificationResult("NOTIFICATION_FAILED", error.args[0], "SMTP credential is unavailable or its backend is unsupported.")
        except smtplib.SMTPAuthenticationError:
            return NotificationResult("NOTIFICATION_FAILED", "SMTP_AUTH_FAILED", "SMTP authentication failed.")
        except smtplib.SMTPRecipientsRefused:
            return NotificationResult("NOTIFICATION_FAILED", "SMTP_RECIPIENT_REJECTED", "SMTP rejected the recipient.")
        except smtplib.SMTPResponseException:
            return NotificationResult("NOTIFICATION_FAILED", "SMTP_REJECTED", "SMTP rejected the operation.")
        except ssl.SSLError:
            return NotificationResult("NOTIFICATION_FAILED", "TLS_FAILED", "SMTP TLS validation or negotiation failed.")
        except Exception:
            if dispatched:
                return NotificationResult("NOTIFICATION_FAILED", "SMTP_OUTCOME_UNKNOWN", "SMTP result is uncertain; no automatic retry was attempted.")
            return NotificationResult("NOTIFICATION_FAILED", "SMTP_CONNECTION_FAILED", "SMTP connection or setup failed.")
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
