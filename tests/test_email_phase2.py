"""Synthetic fixtures only; no real SMTP connection or Keychain access."""

from dataclasses import replace
import getpass
import json
import os
from pathlib import Path
import smtplib
import stat
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import warnings

from autograb.core.models import Product
from autograb.notifications.email import EmailNotifier, NotificationResult, SMTPConfig, safe_invoice_url
from autograb.notifications.setup import EmailSetupError, _save_settings, load_setup, setup_email


SECRET = "fixture-app-password-not-real"
SETTINGS = {
    "host": "smtp.example.com", "username": "sender@example.com", "sender": "sender@example.com",
    "recipient": "recipient@example.com", "tls_mode": "ssl", "port": 465,
    "secret_service": "AutoGrab SMTP", "secret_account": "autograb-fixture", "timeout": 10,
}
PRODUCT = Product("123", "Synthetic annual VPS", "AVAILABLE", [{"cents": 4999, "currency": "USD", "period": "Annually"}],
                  "https://bandwagonhost.com/order/basic")
EVENT = {"id": "fixture-event", "event_type": "RESTOCK"}
URL = "https://bandwagonhost.com/viewinvoice.php?id=456"


def intent():
    # REAL_SITE is an input to the notification validator under an entirely
    # mocked SMTP transport. This is not live evidence or a real merchant order.
    return {
        "id": "fixture-intent", "provider": "bandwagon", "product_id": "123", "state": "PAYMENT_READY", "origin": "REAL",
        "order_id": "789", "invoice_id": "456", "payment_url": URL,
        "verification": {
            "source": "REAL_SITE", "merchant": "bandwagonhost.com", "product_id": "123",
            "order_id": "789", "invoice_id": "456", "payment_url": URL, "invoice_status": "UNPAID",
            "merchant_verified": True, "product_verified": True, "amount_present": True,
            "payment_page_verified": True, "unpaid_verified": True, "amount": "USD 49.99", "billing": "Annually",
        },
    }


def client():
    mock = MagicMock()
    mock.ehlo.return_value = (250, b"fixture")
    mock.send_message.return_value = {}
    return mock


def config():
    return SMTPConfig.from_mapping(SETTINGS, {"SMTP_PASSWORD": SECRET})


class Phase2ConfigTests(unittest.TestCase):
    def test_autograb_aliases_are_sufficient_with_email_username(self):
        conf = SMTPConfig.from_env({"AUTOGRAB_SMTP_HOST": "smtp.example.com", "AUTOGRAB_SMTP_USER": "sender@example.com",
                                    "AUTOGRAB_SMTP_PASSWORD": SECRET, "AUTOGRAB_EMAIL_TO": "recipient@example.com"})
        self.assertIsNone(conf.error_code)
        self.assertEqual(conf.sender, "sender@example.com")
        self.assertNotIn(SECRET, repr(conf))

    def test_legacy_environment_takes_precedence(self):
        conf = SMTPConfig.from_mapping(SETTINGS, {"AUTOGRAB_SMTP_HOST": "fallback.example.com", "SMTP_HOST": "primary.example.com",
                                                    "AUTOGRAB_SMTP_USER": "alias@example.com", "SMTP_USERNAME": "sender@example.com",
                                                    "AUTOGRAB_EMAIL_TO": "alias-to@example.com", "SMTP_RECIPIENT": "recipient@example.com",
                                                    "AUTOGRAB_SMTP_PASSWORD": "fixture-other", "SMTP_PASSWORD": SECRET})
        self.assertEqual(conf.host, "primary.example.com")
        self.assertEqual(conf.username, "sender@example.com")
        self.assertEqual(conf.recipient, "recipient@example.com")
        self.assertEqual(conf._password, SECRET)

    def test_invoice_url_only_accepts_exact_official_matching_id(self):
        self.assertEqual(safe_invoice_url(URL, "456"), URL)
        for invalid in (URL + "&token=fixture", URL + "#fragment", URL.replace("456", "457"),
                        URL.replace("https:", "http:"), URL.replace("bandwagonhost.com", "bandwagonhost.com.evil.example"),
                        "https://bandwagonhost.com/cart.php?a=view", "https://user@bandwagonhost.com/viewinvoice.php?id=456",
                        "https://bandwagonhost.com:443/viewinvoice.php?id=456", "https://bandwagonhost.com/viewinvoice.php?id=0456"):
            with self.subTest(invalid=invalid):
                self.assertEqual(safe_invoice_url(invalid, "456"), "UNAVAILABLE")
        for identifier in (None, True, "0", "456\n", "-1"):
            self.assertEqual(safe_invoice_url(URL, identifier), "UNAVAILABLE")


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_atomic_private_settings_roundtrip_no_secret(self):
        _save_settings(self.root, SETTINGS, replace=False)
        path = self.root / "data/email-settings.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertNotIn(SECRET, path.read_text())
        self.assertEqual(path.stat().st_nlink, 1)
        with patch("keyring.get_keyring") as keychain:
            loaded = load_setup(self.root, base={"host": "legacy.example.com"}, env={"SMTP_HOST": "override.example.com"})
        self.assertEqual(loaded.host, "override.example.com")
        self.assertEqual(loaded.secret_account, "autograb-fixture")
        self.assertFalse(loaded._password)
        keychain.assert_not_called()

    def test_new_settings_never_clobber_existing_without_replace(self):
        _save_settings(self.root, SETTINGS, replace=False)
        with self.assertRaisesRegex(EmailSetupError, "EMAIL_SETTINGS_EXISTS"):
            _save_settings(self.root, {**SETTINGS, "host": "changed.example.com"}, replace=False)
        self.assertEqual(load_setup(self.root, env={}).host, SETTINGS["host"])

    def test_rejects_secret_or_unknown_json_field(self):
        for extra in ("password", "SMTP_PASSWORD", "AUTOGRAB_SMTP_PASSWORD", "cookie", "unexpected"):
            with self.subTest(extra=extra), self.assertRaisesRegex(EmailSetupError, "EMAIL_SETTINGS_INVALID"):
                _save_settings(self.root, {**SETTINGS, extra: SECRET}, replace=False)
        self.assertFalse((self.root / "data/email-settings.json").exists())

    def test_unsafe_symlink_directory_and_settings_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "data").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(EmailSetupError):
            _save_settings(self.root, SETTINGS, replace=False)
        (self.root / "data").unlink()
        (self.root / "data").mkdir()
        target = outside / "original.json"
        target.write_text("original")
        (self.root / "data/email-settings.json").symlink_to(target)
        with self.assertRaises(EmailSetupError):
            load_setup(self.root, env={})
        with self.assertRaises(EmailSetupError):
            _save_settings(self.root, SETTINGS, replace=True)
        self.assertEqual(target.read_text(), "original")

    def test_hardlink_and_world_readable_settings_rejected(self):
        _save_settings(self.root, SETTINGS, replace=False)
        path = self.root / "data/email-settings.json"
        os.link(path, self.root / "other")
        with self.assertRaises(EmailSetupError):
            load_setup(self.root, env={})
        (self.root / "other").unlink()
        path.chmod(0o644)
        with self.assertRaises(EmailSetupError):
            load_setup(self.root, env={})

    def test_missing_setup_loads_legacy_without_keychain(self):
        with patch("keyring.get_keyring") as keychain:
            loaded = load_setup(self.root, SETTINGS, env={})
        self.assertEqual(loaded.host, SETTINGS["host"])
        keychain.assert_not_called()

    def test_failed_atomic_save_preserves_old_settings(self):
        _save_settings(self.root, SETTINGS, replace=False)
        with patch("autograb.notifications.setup.os.replace", side_effect=OSError(SECRET)):
            with self.assertRaisesRegex(EmailSetupError, "EMAIL_SETTINGS_SAVE_FAILED"):
                _save_settings(self.root, {**SETTINGS, "host": "new.example.com"}, replace=True)
        self.assertEqual(load_setup(self.root, env={}).host, SETTINGS["host"])
        self.assertEqual([path.name for path in (self.root / "data").iterdir()], ["email-settings.json"])


class SetupWizardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.backend = MagicMock()
        self.answers = ["smtp.example.com", "sender@example.com", "", "recipient@example.com", "465", "SAVE", "skip"]

    async def wizard(self, answers=None, secret=None):
        ask = MagicMock(side_effect=self.answers if answers is None else answers)
        password_prompt = MagicMock(return_value=SECRET) if secret is None else secret
        output = MagicMock()
        with patch("autograb.notifications.setup._keychain_backend", return_value=self.backend):
            result = await setup_email(self.root, input_fn=ask, getpass_fn=password_prompt, output_fn=output)
        return result, ask, password_prompt, output

    async def test_password_only_uses_non_echo_prompt_and_keychain(self):
        result, ask, secret, output = await self.wizard()
        self.assertEqual(result.status, "CONFIGURED")
        self.assertIsNone(result.notification)
        secret.assert_called_once()
        self.assertFalse(any("password" in call.args[0] for call in ask.call_args_list))
        self.backend.set_password.assert_called_once()
        service, account, value = self.backend.set_password.call_args.args
        self.assertEqual(service, "AutoGrab SMTP")
        self.assertTrue(account.startswith("autograb-"))
        self.assertEqual(value, SECRET)
        self.backend.get_password.assert_not_called()
        self.assertNotIn(SECRET, repr(output.mock_calls))
        self.assertNotIn(SECRET, (self.root / "data/email-settings.json").read_text())

    async def test_send_requires_explicit_confirmation(self):
        with patch.object(EmailNotifier, "send_live_test", new_callable=AsyncMock) as send:
            await self.wizard()
        send.assert_not_called()

    async def test_confirmed_send_uses_new_test_and_reports_acceptance(self):
        accepted = NotificationResult("SMTP_ACCEPTED", detail="inbox unverified")
        with patch.object(EmailNotifier, "send_live_test", new_callable=AsyncMock, return_value=accepted) as send:
            result, _, _, output = await self.wizard(self.answers[:-1] + ["SEND"])
        send.assert_awaited_once()
        self.assertEqual(result.notification.status, "SMTP_ACCEPTED")
        self.assertIn("不代表已送达收件箱", repr(output.mock_calls))

    async def test_existing_configuration_requires_explicit_replace_before_secret(self):
        _save_settings(self.root, SETTINGS, replace=False)
        result, _, secret, _ = await self.wizard(["no"])
        self.assertEqual(result.status, "CANCELLED")
        secret.assert_not_called()
        self.backend.set_password.assert_not_called()
        self.assertEqual(load_setup(self.root, env={}).secret_account, SETTINGS["secret_account"])

    async def test_user_can_cancel_before_keychain_write(self):
        result, _, _, _ = await self.wizard(self.answers[:5] + ["no"])
        self.assertEqual(result.status, "CANCELLED")
        self.backend.set_password.assert_not_called()
        self.assertFalse((self.root / "data/email-settings.json").exists())

    async def test_reject_getpass_echo_fallback(self):
        def insecure(_):
            warnings.warn("cannot control echo", getpass.GetPassWarning)
            return SECRET
        result, _, _, _ = await self.wizard(secret=insecure)
        self.assertEqual(result.error_code, "SECURE_TERMINAL_REQUIRED")
        self.backend.set_password.assert_not_called()

    async def test_invalid_nonsecret_settings_never_request_password(self):
        result, _, secret, _ = await self.wizard(["smtp.example.com\nBcc: injected", "sender@example.com", "", "recipient@example.com", "465"])
        self.assertEqual(result.error_code, "EMAIL_SETTINGS_INVALID")
        secret.assert_not_called()

    async def test_keychain_failure_does_not_write_file_or_expose_error(self):
        self.backend.set_password.side_effect = OSError(SECRET)
        result, _, _, output = await self.wizard()
        self.assertEqual(result.error_code, "KEYCHAIN_SAVE_FAILED")
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, repr(output.mock_calls))
        self.assertFalse((self.root / "data/email-settings.json").exists())

    async def test_save_failure_cleans_only_new_credential(self):
        with patch("autograb.notifications.setup._save_settings", side_effect=EmailSetupError("EMAIL_SETTINGS_SAVE_FAILED")):
            result, _, _, _ = await self.wizard()
        self.assertEqual(result.error_code, "EMAIL_SETTINGS_SAVE_FAILED")
        service, account, _ = self.backend.set_password.call_args.args
        self.backend.delete_password.assert_called_once_with(service, account)

    async def test_uncertain_save_preserves_credential_for_potentially_committed_settings(self):
        with patch("autograb.notifications.setup._save_settings", side_effect=EmailSetupError("EMAIL_SETTINGS_OUTCOME_UNKNOWN")):
            result, _, _, _ = await self.wizard()
        self.assertEqual(result.error_code, "EMAIL_SETTINGS_OUTCOME_UNKNOWN")
        self.backend.delete_password.assert_not_called()

    async def test_wrong_keychain_backend_rejected_before_input(self):
        ask, secret = MagicMock(), MagicMock()
        backend = MagicMock()
        with patch("sys.platform", "darwin"), patch("keyring.get_keyring", return_value=backend):
            result = await setup_email(self.root, input_fn=ask, getpass_fn=secret)
        self.assertEqual(result.error_code, "UNSAFE_KEYRING_BACKEND")
        ask.assert_not_called()
        secret.assert_not_called()
        backend.get_password.assert_not_called()
        backend.set_password.assert_not_called()

    async def test_smtp_failed_send_keeps_config_and_does_not_retry(self):
        rejected = NotificationResult("NOTIFICATION_FAILED", "SMTP_AUTH_FAILED")
        with patch.object(EmailNotifier, "send_live_test", new_callable=AsyncMock, return_value=rejected) as send:
            result, _, _, _ = await self.wizard(self.answers[:-1] + ["SEND"])
        self.assertEqual(result.status, "CONFIGURED")
        self.assertEqual(result.notification.error_code, "SMTP_AUTH_FAILED")
        send.assert_awaited_once()
        self.assertIsNone(load_setup(self.root, env={}).error_code)


class PaymentEmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase2_test_subject_and_honest_delivery(self):
        smtp = client()
        with patch("smtplib.SMTP_SSL", return_value=smtp):
            result = await EmailNotifier(config()).send_live_test()
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "[AUTOGRAB TEST] Email notification working")
        self.assertIn("does not confirm DELIVERED TO INBOX", message.get_content())

    async def test_payment_ready_full_content_official_url_and_stable_id(self):
        smtp = client()
        timing = {"marks": {"T0": {"utc": "2026-09-22T01:00:00.000Z"}, "T6": {"utc": "2026-09-22T01:00:02.000Z"},
                            "T8": {"utc": "2026-09-22T01:00:03.000Z"}}, "durations_ms": {"detection_to_payment_ready": 3000}}
        with patch("smtplib.SMTP_SSL", return_value=smtp):
            for _ in range(2):
                result = await EmailNotifier(config()).send_payment_ready(PRODUCT, EVENT, intent(), timing)
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        messages = [call.args[0] for call in smtp.send_message.call_args_list]
        self.assertEqual(messages[0]["Message-ID"], messages[1]["Message-ID"])
        self.assertEqual(messages[0]["Subject"], "🚨🚨 [PAY NOW] BandwagonHost VPS 已进入付款阶段")
        body = messages[0].get_content()
        for expected in (URL, "Login may be required", "RESTOCK", "USD 49.99", "Annually", "Order ID:\n789", "Invoice ID:\n456",
                         "Status:\nPAYMENT_READY", "3.000 sec", "Payment deadline:\nUNKNOWN", "AutoGrab has not submitted payment"):
            self.assertIn(expected, body)
        self.assertNotIn("127.0.0.1", body)
        self.assertNotIn(SECRET, body)

    async def test_all_payment_evidence_flags_required_before_transport(self):
        for key in ("merchant_verified", "product_verified", "amount_present", "payment_page_verified", "unpaid_verified"):
            value = intent()
            value["verification"][key] = False
            with self.subTest(key=key), patch("smtplib.SMTP_SSL") as smtp:
                result = await EmailNotifier(config()).send_payment_ready(PRODUCT, EVENT, value, {})
                self.assertEqual(result.error_code, "PAYMENT_NOT_VERIFIED")
                smtp.assert_not_called()

    async def test_invalid_paid_or_mismatched_receipts_rejected(self):
        for key, replacement in (("invoice_status", "PAID"), ("invoice_status", "Cancelled"), ("invoice_status", "Expired"),
                                 ("product_id", "999"), ("order_id", "888"), ("invoice_id", "999"),
                                 ("merchant", "other.example"), ("payment_url", URL + "&token=fixture"),
                                 ("source", "MOCK"), ("source", "SIMULATED")):
            value = intent()
            value["verification"][key] = replacement
            with self.subTest(key=key, replacement=replacement), patch("smtplib.SMTP_SSL") as smtp:
                result = await EmailNotifier(config()).send_payment_ready(PRODUCT, EVENT, value, {})
                self.assertEqual(result.error_code, "PAYMENT_NOT_VERIFIED")
                smtp.assert_not_called()

    async def test_cart_and_incomplete_intents_cannot_trigger_pay_now(self):
        for change in ({"state": "CART_READY"}, {"order_id": None}, {"invoice_id": None}, {"verification": {}},
                       {"payment_url": "https://bandwagonhost.com/cart.php?a=view"}, {"provider": "other"}, {"product_id": "999"},
                       {"origin": "SIMULATED"}):
            with self.subTest(change=change), patch("smtplib.SMTP_SSL") as smtp:
                result = await EmailNotifier(config()).send_payment_ready(PRODUCT, EVENT, {**intent(), **change}, {})
                self.assertEqual(result.error_code, "PAYMENT_NOT_VERIFIED")
                smtp.assert_not_called()

    async def test_waiting_for_user_valid_receipt_can_notify(self):
        smtp = client()
        with patch("smtplib.SMTP_SSL", return_value=smtp):
            result = await EmailNotifier(config()).send_payment_ready(PRODUCT, EVENT, {**intent(), "state": "WAITING_FOR_USER"}, {})
        self.assertEqual(result.status, "SMTP_ACCEPTED")

    async def test_payment_notification_failure_is_uncertain_and_not_retried(self):
        smtp = client()
        smtp.send_message.side_effect = smtplib.SMTPServerDisconnected(SECRET)
        with patch("smtplib.SMTP_SSL", return_value=smtp):
            result = await EmailNotifier(config()).send_payment_ready(PRODUCT, EVENT, intent(), {})
        self.assertEqual(result.error_code, "SMTP_OUTCOME_UNKNOWN")
        self.assertNotIn(SECRET, repr(result))
        smtp.send_message.assert_called_once()

    async def test_login_required_notification(self):
        smtp = client()
        with patch("smtplib.SMTP_SSL", return_value=smtp):
            result = await EmailNotifier(config()).send_session_required("fixture-session")
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        body = smtp.send_message.call_args.args[0].get_content()
        self.assertIn("LOGIN_REQUIRED", body)
        self.assertIn("New order creation is blocked", body)

    async def test_unknown_session_notice_refused_and_unconfigured_isolated(self):
        with patch("smtplib.SMTP_SSL") as smtp:
            self.assertEqual((await EmailNotifier(config()).send_session_required("x", "secret raw page")).error_code, "INVALID_NOTICE")
            self.assertEqual((await EmailNotifier(SMTPConfig.from_env({})).send_session_required("x")).status, "NOT_CONFIGURED")
            self.assertEqual((await EmailNotifier(SMTPConfig.from_env({})).send_payment_ready(PRODUCT, EVENT, intent(), {})).status, "NOT_CONFIGURED")
        smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
