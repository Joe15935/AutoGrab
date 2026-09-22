"""No network or user's Keychain is accessed by these notification tests."""

from dataclasses import replace
import smtplib
import ssl
import sys
import unittest
from unittest.mock import MagicMock, patch

from autograb.core.models import Product
from autograb.notifications.email import EmailNotifier, KEYCHAIN_SERVICE, SMTPConfig, safe_public_url


TEST_SECRET = "test-only-not-a-real-secret"


def configured(**overrides):
    config = SMTPConfig.from_mapping(
        {
            "host": "smtp.example.com",
            "port": 465,
            "username": "sender@example.com",
            "sender": "sender@example.com",
            "recipient": "recipient@example.com",
        },
        {"SMTP_PASSWORD": TEST_SECRET},
    )
    return replace(config, **overrides)


def smtp_client():
    client = MagicMock()
    client.ehlo.return_value = (250, b"hello")
    client.send_message.return_value = {}
    return client


PRODUCT = Product(
    product_id="fixture-123",
    name="Fixture annual promotion",
    availability="AVAILABLE",
    prices=[{"price": "123.45", "currency": "USD", "billing_cycle": "annually"}],
    product_url="https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9?token=discard-me",
)
EVENT = {"id": "fixture-event-1", "event_type": "RESTOCK", "private": "must-not-appear"}
TIMING = {
    "marks": {
        "T0": {"utc": "2026-09-22T01:00:00.123Z"},
        "T3": {"utc": "2026-09-22T01:00:01.234Z"},
        "T4": {"utc": "2026-09-22T01:00:01.345Z"},
    },
    "durations_ms": {"detection_to_boundary_ms": 1222},
}
BOUNDARY = {
    "status": "DRY_RUN_BOUNDARY_REACHED",
    "cart_url": "https://bandwagonhost.com/cart.php?a=view&token=discard-me&address=private#session",
    "private": "must-not-appear",
}


class ConfigTests(unittest.TestCase):
    def test_email_address_aliases_and_smtp_precedence(self):
        aliases = {"EMAIL_FROM": "alias@example.com", "EMAIL_TO": "alias-to@example.com"}
        config = SMTPConfig.from_env(aliases)
        self.assertEqual(config.sender, "alias@example.com")
        self.assertEqual(config.recipient, "alias-to@example.com")
        config = SMTPConfig.from_env({**aliases, "SMTP_SENDER": "smtp@example.com", "SMTP_RECIPIENT": "smtp-to@example.com"})
        self.assertEqual(config.sender, "smtp@example.com")
        self.assertEqual(config.recipient, "smtp-to@example.com")

    def test_environment_override_and_no_secret_repr(self):
        config = SMTPConfig.from_mapping(
            {"smtp": {"host": "old.example.com", "port": 587, "tls_mode": "starttls"}},
            {
                "SMTP_HOST": "new.example.com",
                "SMTP_USERNAME": "sender@example.com",
                "SMTP_SENDER": "sender@example.com",
                "SMTP_RECIPIENT": "recipient@example.com",
                "SMTP_PASSWORD": TEST_SECRET,
            },
        )
        self.assertIsNone(config.error_code)
        self.assertEqual(config.host, "new.example.com")
        self.assertEqual(config.port, 587)
        self.assertNotIn(TEST_SECRET, repr(config))
        self.assertNotIn("sender@example.com", repr(config))

    def test_missing_or_bad_settings_never_raise(self):
        self.assertEqual(SMTPConfig.from_env({}).error_code, "NOT_CONFIGURED")
        self.assertEqual(SMTPConfig.from_env({"SMTP_PORT": "nope"}).error_code, "CONFIG_INVALID")
        for change in (
            {"port": 25},
            {"tls_mode": "plain"},
            {"tls_mode": "starttls", "port": 465},
            {"host": "smtp.example.com\r\ninjected"},
            {"host": "https://smtp.example.com"},
            {"sender": "sender@example.com\r\nBcc: attacker@example.com"},
            {"recipient": "one@example.com,two@example.com"},
            {"username": "sender\nuser"},
            {"timeout": 0},
            {"timeout": float("nan")},
            {"secret_service": "Other application"},
        ):
            with self.subTest(change=change):
                self.assertEqual(configured(**change).error_code, "CONFIG_INVALID")

    def test_rejects_password_in_mapping(self):
        self.assertEqual(
            SMTPConfig.from_mapping({"password": TEST_SECRET}, {}).error_code,
            "CONFIG_INVALID",
        )

    def test_missing_secret_does_not_assume_keychain(self):
        self.assertEqual(configured(_password="").error_code, "NOT_CONFIGURED")
        with patch("keyring.get_keyring") as keyring:
            notifier = EmailNotifier(configured(_password="", secret_account="explicit-test-account"))
            self.assertTrue(notifier.configured)
            keyring.assert_not_called()


class URLTests(unittest.TestCase):
    def test_explicit_catalog_tiers_and_configuration_handoff(self):
        for tier in ("basic", "ultra", "ecommerce", "ecommerce-sla-elevated"):
            url = f"https://bandwagonhost.com/order/{tier}/Los%20Angeles/USCA_9"
            self.assertEqual(safe_public_url(url + "?session=discard"), url)
        self.assertEqual(safe_public_url("https://bandwagonhost.com/cart.php?a=confproduct&i=0&token=discard"), "https://bandwagonhost.com/cart.php?a=view")
        self.assertEqual(safe_public_url("https://bandwagonhost.com/order/private/token"), "UNAVAILABLE")

    def test_redacts_queries_and_preserves_only_read_cart_action(self):
        self.assertEqual(safe_public_url(BOUNDARY["cart_url"]), "https://bandwagonhost.com/cart.php?a=view")
        self.assertEqual(
            safe_public_url(PRODUCT.product_url),
            "https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9",
        )

    def test_rejects_sensitive_paths_wrong_hosts_and_write_actions(self):
        for url in (
            "https://bandwagonhost.com/cart.php?a=add&pid=123",
            "https://bandwagonhost.com/cart.php?a=checkout",
            "https://bandwagonhost.com/cart.php?a=view&a=add",
            "https://bandwagonhost.com/viewinvoice.php?id=123",
            "https://bandwagonhost.com/order/ecommerce/%2e%2e/login",
            "https://bandwagonhost.com/order/ecommerce/%252e%252e/login",
            "https://bandwagonhost.com/order/ecommerce/%0aBad",
            "https://bandwagonhost.com.evil.example/cart.php?a=view",
            "https://user:private@bandwagonhost.com/cart.php?a=view",
            "http://bandwagonhost.com/cart.php?a=view",
            "https://bandwagonhost.com:444/cart.php?a=view",
            "javascript:alert(1)",
            "https://bandwagonhost.com/cart.php?a=view\nBcc: malicious",
        ):
            with self.subTest(url=url):
                self.assertEqual(safe_public_url(url), "UNAVAILABLE")


class EmailTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_integer_cents_prices_and_missing_cycles(self):
        product = replace(PRODUCT, prices=[
            {"cents": 99999999, "currency": "USD", "period": "Annually"},
            {"cents": 0, "currency": "USD", "period": "Monthly"},
            {"cents": -1, "currency": "USD", "period": "Quarterly"},
            {"cents": None, "currency": "USD", "period": None},
        ])
        client = smtp_client()
        with patch("smtplib.SMTP_SSL", return_value=client):
            result = await EmailNotifier(configured()).send_event(product, EVENT, TIMING, BOUNDARY)
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        body = client.send_message.call_args.args[0].get_content()
        self.assertIn("USD 999999.99; USD 0.00; USD UNAVAILABLE; USD UNKNOWN", body)
        self.assertIn("Annually; Monthly; Quarterly; UNKNOWN", body)
        self.assertIn("does not reserve inventory", body)

    async def test_unconfigured_never_opens_connection_or_keychain(self):
        notifier = EmailNotifier(SMTPConfig.from_env({}))
        with patch("smtplib.SMTP_SSL") as smtp, patch("keyring.get_keyring") as keyring:
            self.assertEqual((await notifier.send_test()).status, "NOT_CONFIGURED")
            self.assertEqual((await notifier.send_event(PRODUCT, EVENT, TIMING, BOUNDARY)).status, "NOT_CONFIGURED")
            smtp.assert_not_called()
            keyring.assert_not_called()

    async def test_invalid_headers_are_rejected_before_transport(self):
        notifier = EmailNotifier(configured(recipient="receiver@example.com\nBcc: other@example.com"))
        with patch("smtplib.SMTP_SSL") as smtp:
            result = await notifier.send_test()
        self.assertEqual(result.error_code, "CONFIG_INVALID")
        smtp.assert_not_called()

    async def test_tls_ssl_and_test_message(self):
        client = smtp_client()
        with patch("smtplib.SMTP_SSL", return_value=client) as smtp:
            result = await EmailNotifier(configured()).send_test()
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        self.assertIn("unverified", result.detail)
        context = smtp.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(smtp.call_args.kwargs["timeout"], 10)
        client.login.assert_called_once_with("sender@example.com", TEST_SECRET)
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "AutoGrab Email Test")
        self.assertIn("EMAIL SYSTEM WORKING", message.get_content())
        self.assertIn("DRY RUN", message.get_content())
        self.assertNotIn(TEST_SECRET, message.as_string())
        client.set_debuglevel.assert_not_called()

    async def test_starttls_precedes_authentication(self):
        client = smtp_client()
        with patch("smtplib.SMTP", return_value=client), patch("smtplib.SMTP_SSL") as implicit:
            result = await EmailNotifier(configured(tls_mode="starttls", port=587)).send_test()
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        self.assertEqual([call[0] for call in client.mock_calls], ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"])
        context = client.starttls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        implicit.assert_not_called()

    async def test_failed_starttls_does_not_send_or_authenticate(self):
        client = smtp_client()
        client.starttls.side_effect = ssl.SSLError(TEST_SECRET)
        with patch("smtplib.SMTP", return_value=client):
            result = await EmailNotifier(configured(tls_mode="starttls", port=587)).send_test()
        self.assertEqual(result.error_code, "TLS_FAILED")
        self.assertNotIn(TEST_SECRET, repr(result))
        client.login.assert_not_called()
        client.send_message.assert_not_called()

    async def test_full_event_format_and_stable_id(self):
        client = smtp_client()
        with patch("smtplib.SMTP_SSL", return_value=client):
            notifier = EmailNotifier(configured())
            for _ in range(2):
                result = await notifier.send_event(PRODUCT, EVENT, TIMING, BOUNDARY)
                self.assertEqual(result.status, "SMTP_ACCEPTED")
        messages = [call.args[0] for call in client.send_message.call_args_list]
        self.assertEqual(messages[0]["Message-ID"], messages[1]["Message-ID"])
        body = messages[0].get_content()
        for text in ("RESTOCK", PRODUCT.name, "USD 123.45", "annually", "1.222 sec", "2026-09-22T01:00:01.345Z", "DRY_RUN_BOUNDARY_REACHED"):
            self.assertIn(text, body)
        for text in ("discard-me", "must-not-appear", "address=", "#session", TEST_SECRET):
            self.assertNotIn(text, body)
        self.assertNotIn("Payment URL", body)

    async def test_different_generations_get_distinct_message_ids(self):
        client = smtp_client()
        with patch("smtplib.SMTP_SSL", return_value=client):
            notifier = EmailNotifier(configured())
            await notifier.send_event(PRODUCT, EVENT, TIMING, BOUNDARY)
            await notifier.send_event(PRODUCT, {**EVENT, "id": "fixture-event-2"}, TIMING, BOUNDARY)
        messages = [call.args[0] for call in client.send_message.call_args_list]
        self.assertNotEqual(messages[0]["Message-ID"], messages[1]["Message-ID"])

    async def test_missing_timing_is_unknown_not_fabricated(self):
        client = smtp_client()
        with patch("smtplib.SMTP_SSL", return_value=client):
            await EmailNotifier(configured()).send_event(PRODUCT, EVENT, {}, {})
        body = client.send_message.call_args.args[0].get_content()
        self.assertIn("Total:\nUNKNOWN", body)
        self.assertIn("Status:\nUNKNOWN", body)
        self.assertIn("Detected:\nNOT REACHED", body)

    async def test_missing_event_id_prevents_sending(self):
        with patch("smtplib.SMTP_SSL") as smtp:
            result = await EmailNotifier(configured()).send_event(PRODUCT, {}, TIMING, BOUNDARY)
        self.assertEqual(result.error_code, "INVALID_EVENT")
        smtp.assert_not_called()

    async def test_smtp_auth_failure_isolated_and_redacted(self):
        client = smtp_client()
        client.login.side_effect = smtplib.SMTPAuthenticationError(535, TEST_SECRET.encode())
        with patch("smtplib.SMTP_SSL", return_value=client):
            result = await EmailNotifier(configured()).send_event(PRODUCT, EVENT, TIMING, BOUNDARY)
        self.assertEqual(result.status, "NOTIFICATION_FAILED")
        self.assertEqual(result.error_code, "SMTP_AUTH_FAILED")
        self.assertNotIn(TEST_SECRET, repr(result))
        client.send_message.assert_not_called()

    async def test_uncertain_data_result_is_not_retried(self):
        client = smtp_client()
        client.send_message.side_effect = smtplib.SMTPServerDisconnected(TEST_SECRET)
        with patch("smtplib.SMTP_SSL", return_value=client):
            result = await EmailNotifier(configured()).send_event(PRODUCT, EVENT, TIMING, BOUNDARY)
        self.assertEqual(result.status, "NOTIFICATION_FAILED")
        self.assertEqual(result.error_code, "SMTP_OUTCOME_UNKNOWN")
        self.assertNotIn(TEST_SECRET, repr(result))
        client.send_message.assert_called_once()

    async def test_acceptance_survives_quit_failure(self):
        client = smtp_client()
        client.quit.side_effect = OSError(TEST_SECRET)
        with patch("smtplib.SMTP_SSL", return_value=client):
            result = await EmailNotifier(configured()).send_test()
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        client.close.assert_called_once()

    async def test_recipient_rejection_is_not_acceptance(self):
        client = smtp_client()
        client.send_message.return_value = {"recipient@example.com": (550, b"rejected")}
        with patch("smtplib.SMTP_SSL", return_value=client):
            result = await EmailNotifier(configured()).send_test()
        self.assertEqual(result.error_code, "SMTP_RECIPIENT_REJECTED")

    async def test_connection_failure_redacts_exception(self):
        with patch("smtplib.SMTP_SSL", side_effect=OSError(TEST_SECRET)):
            result = await EmailNotifier(configured()).send_test()
        self.assertEqual(result.error_code, "SMTP_CONNECTION_FAILED")
        self.assertNotIn(TEST_SECRET, repr(result))

    async def test_unsupported_keychain_backend_not_read(self):
        backend = MagicMock()
        with patch("sys.platform", "darwin"), patch("keyring.get_keyring", return_value=backend), patch("smtplib.SMTP_SSL") as smtp:
            result = await EmailNotifier(configured(_password="", secret_account="explicit-test-account")).send_test()
        self.assertEqual(result.error_code, "UNSAFE_KEYRING_BACKEND")
        backend.get_password.assert_not_called()
        smtp.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "macOS secure backend test")
    async def test_only_explicit_autograb_keychain_account_read(self):
        from keyring.backends.macOS import Keyring

        backend = Keyring()
        client = smtp_client()
        with patch("keyring.get_keyring", return_value=backend), patch.object(Keyring, "get_password", return_value=TEST_SECRET) as read, patch("smtplib.SMTP_SSL", return_value=client):
            result = await EmailNotifier(configured(_password="", secret_account="explicit-test-account")).send_test()
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        read.assert_called_once_with(KEYCHAIN_SERVICE, "explicit-test-account")


if __name__ == "__main__":
    unittest.main()
