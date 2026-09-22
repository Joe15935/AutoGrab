"""Phase 2 controls on temporary data. All network/browser/Keychain calls mocked."""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from autograb.core.config import Config
from autograb.core.events import EventLog
from autograb.core.live import LiveGuard, Preflight, REQUIRED_CHECKS, SMTPProof, signal_present
from autograb.core.lock import ProcessLock
from autograb.core.models import Product
from autograb.notifications.email import NotificationResult, SMTPConfig
from autograb.notifications.setup import SetupResult
from autograb.phase2_cli import register_commands, run_phase2
from autograb.storage.database import Store


PRODUCT = Product("87", "Synthetic CLI fixture", "AVAILABLE", [{"cents": 4999, "currency": "USD", "period": "Annually"}],
                  "https://bandwagonhost.com/order/basic")


def args(*parts):
    parser = argparse.ArgumentParser()
    register_commands(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(parts)


class Phase2CliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(root=self.root)
        self.config.prepare()
        self.addCleanup(self.temp.cleanup)
        self.browser = MagicMock(context=None)
        self.browser.close = AsyncMock()
        self.browser.start = AsyncMock()
        self.provider = MagicMock()
        self.provider.discover_products = AsyncMock(return_value=[PRODUCT])
        self.provider.inspect_session = AsyncMock(return_value={"status": "SESSION_VALID"})
        self.provider.submit_order = AsyncMock()
        self.notifier = MagicMock(configured=False)
        self.notifier.send_live_test = AsyncMock(return_value=NotificationResult("SMTP_ACCEPTED"))
        self.startPatch = self.enterContext(patch("autograb.phase2_cli.BrowserManager", return_value=self.browser))
        self.enterContext(patch("autograb.phase2_cli.BandwagonPaymentProvider", return_value=self.provider))
        self.enterContext(patch("autograb.phase2_cli.EmailNotifier", return_value=self.notifier))
        self.load = self.enterContext(patch("autograb.phase2_cli.load_setup", return_value=SMTPConfig.from_env({})))

    async def run_cli(self, *parts):
        output = io.StringIO()
        with redirect_stdout(output):
            code = await run_phase2(args(*parts), self.config)
        return code, output.getvalue()

    def baseline(self):
        with Store(self.root / "data/autograb.sqlite3") as store:
            store.ingest([PRODUCT])

    async def test_stop_only_stops_monitoring_and_bypasses_busy_process(self):
        with ProcessLock(self.root / "data/autograb.lock"):
            code, output = await self.run_cli("stop")
        self.assertEqual(code, 0)
        self.assertTrue(signal_present(self.root / "data", "stop_monitoring"))
        self.assertFalse(signal_present(self.root / "data", "disarm"))
        self.assertIn("PRESERVED", output)
        self.startPatch.assert_not_called()

    async def test_disarm_only_revokes_order_creation_and_bypasses_busy_process(self):
        with ProcessLock(self.root / "data/autograb.lock"):
            code, _ = await self.run_cli("disarm")
        self.assertEqual(code, 0)
        self.assertTrue(signal_present(self.root / "data", "disarm"))
        self.assertFalse(signal_present(self.root / "data", "stop_monitoring"))
        self.startPatch.assert_not_called()

    async def test_stop_all_sets_both_signals_without_deleting_existing_order_record(self):
        record = self.root / "data/existing-order-fixture.json"
        record.write_text('{"fixture":"preserved"}')
        code, _ = await self.run_cli("stop-all")
        self.assertEqual(code, 0)
        self.assertTrue(signal_present(self.root / "data", "disarm"))
        self.assertTrue(signal_present(self.root / "data", "stop_monitoring"))
        self.assertEqual(record.read_text(), '{"fixture":"preserved"}')

    async def test_explicit_resume_and_clear_only_remove_their_respective_signal(self):
        await self.run_cli("stop-all")
        code, output = await self.run_cli("resume-monitoring")
        self.assertEqual(code, 0)
        self.assertFalse(signal_present(self.root / "data", "stop_monitoring"))
        self.assertTrue(signal_present(self.root / "data", "disarm"))
        self.assertIn("LIVE remains DISARMED", output)
        self.assertEqual((await self.run_cli("clear-disarm"))[0], 0)
        self.assertFalse(signal_present(self.root / "data", "disarm"))
        self.startPatch.assert_not_called()

    async def test_default_dry_run_refuses_arm_before_network_or_email(self):
        self.assertEqual(args("arm").mode, "DRY_RUN")
        code, output = await self.run_cli("arm")
        self.assertEqual(code, 2)
        self.assertIn("DRY_RUN_CANNOT_ARM", output)
        self.provider.discover_products.assert_not_awaited()
        self.notifier.send_live_test.assert_not_awaited()
        self.provider.submit_order.assert_not_awaited()
        self.browser.close.assert_awaited_once()

    async def test_unconfigured_email_hard_refuses_live_arm_without_network(self):
        code, output = await self.run_cli("arm", "--mode", "LIVE", "--hours", "1")
        self.assertEqual(code, 2)
        self.assertIn("EMAIL_NOT_CONFIGURED", output)
        self.assertIn("USER_ACTION_REQUIRED", output)
        self.provider.discover_products.assert_not_awaited()
        self.notifier.send_live_test.assert_not_awaited()
        self.provider.submit_order.assert_not_awaited()

    async def test_stopped_monitoring_prevents_arm_before_network(self):
        await self.run_cli("stop")
        self.notifier.configured = True
        code, output = await self.run_cli("arm", "--mode", "LIVE")
        self.assertEqual(code, 2)
        self.assertIn("MONITORING_STOPPED", output)
        self.provider.discover_products.assert_not_awaited()

    async def test_invalid_expiry_never_connects(self):
        self.notifier.configured = True
        for options in (("--hours", "nan"), ("--hours", "0"), ("--hours", "25"),
                        ("--until", "not-a-date"), ("--until", "2099-01-01T00:00:00")):
            with self.subTest(options=options):
                code, output = await self.run_cli("arm", "--mode", "LIVE", *options)
                self.assertEqual(code, 2)
                self.assertIn("ARM_EXPIRY_INVALID", output)
        self.provider.discover_products.assert_not_awaited()
        self.notifier.send_live_test.assert_not_awaited()

    async def test_real_adapter_boundary_remains_blocked_even_when_other_checks_pass(self):
        self.baseline()
        self.notifier.configured = True
        code, output = await self.run_cli("arm", "--mode", "LIVE", "--hours", "1")
        self.assertEqual(code, 2)
        self.assertIn("UNVERIFIED_AUTHENTICATED_ACCOUNT", output)
        self.assertIn("LIVE_PREFLIGHT_FAILED", output)
        self.assertIn('"armed": false', output)
        self.notifier.send_live_test.assert_awaited_once()
        self.provider.submit_order.assert_not_awaited()

    async def test_smtp_failure_refuses_arm(self):
        self.baseline()
        self.notifier.configured = True
        self.notifier.send_live_test.return_value = NotificationResult("NOTIFICATION_FAILED", "SMTP_AUTH_FAILED")
        code, output = await self.run_cli("arm", "--mode", "LIVE")
        self.assertEqual(code, 2)
        self.assertIn('"email": "NOTIFICATION_FAILED"', output)
        self.assertIn("LIVE_PREFLIGHT_FAILED", output)
        self.provider.submit_order.assert_not_awaited()

    async def test_offline_preflight_does_not_start_browser_or_send_even_with_test_flag(self):
        self.baseline()
        self.notifier.configured = True
        code, output = await self.run_cli("preflight", "--offline", "--test-email")
        self.assertEqual(code, 2)
        self.assertIn('"database": "PASS"', output)
        self.assertIn('"baseline": "PASS"', output)
        self.assertIn('"browser": "NOT_TESTED"', output)
        self.assertIn('"email": "CONFIGURED_UNTESTED"', output)
        self.browser.start.assert_not_awaited()
        self.provider.discover_products.assert_not_awaited()
        self.provider.inspect_session.assert_not_awaited()
        self.notifier.send_live_test.assert_not_awaited()

    async def test_control_status_discloses_command_scope_and_unobserved_running_process(self):
        code, output = await self.run_cli("control-status")
        self.assertEqual(code, 0)
        self.assertIn('"scope": "THIS_COMMAND_PROCESS"', output)
        self.assertIn('"cross_process_arm_status": "NOT_OBSERVED"', output)
        self.assertIn('"dashboard": "NOT_IMPLEMENTED"', output)
        self.assertIn('"order_adapter": "AUTHENTICATED_BOUNDARY_UNVERIFIED"', output)
        self.provider.discover_products.assert_not_awaited()

    async def test_configure_email_recognizes_configured_success_and_optional_send(self):
        for notification in (None, NotificationResult("SMTP_ACCEPTED")):
            with self.subTest(notification=notification):
                with patch("autograb.phase2_cli.setup_email", new_callable=AsyncMock,
                           return_value=SetupResult("CONFIGURED", notification=notification)) as setup:
                    code, output = await self.run_cli("configure-email")
                self.assertEqual(code, 0)
                setup.assert_awaited_once_with(self.root)
                self.assertIn("CONFIGURED", output)
        self.startPatch.assert_not_called()
        self.load.assert_not_called()

    async def test_configure_email_failed_send_returns_failure_but_keeps_configuration_status(self):
        result = SetupResult("CONFIGURED", notification=NotificationResult("NOTIFICATION_FAILED", "SMTP_AUTH_FAILED"))
        with patch("autograb.phase2_cli.setup_email", new_callable=AsyncMock, return_value=result):
            code, output = await self.run_cli("configure-email")
        self.assertEqual(code, 2)
        self.assertIn("CONFIGURED", output)
        self.assertIn("SMTP_AUTH_FAILED", output)

    async def test_configure_email_failure_and_cancellation_codes(self):
        for result, expected in ((SetupResult("FAILED", "KEYCHAIN_SAVE_FAILED"), 2), (SetupResult("CANCELLED"), 0)):
            with self.subTest(result=result), patch("autograb.phase2_cli.setup_email", new_callable=AsyncMock, return_value=result):
                code, _ = await self.run_cli("configure-email")
                self.assertEqual(code, expected)

    async def test_configure_menu_routes_email_without_browser(self):
        with patch("builtins.input", return_value="1"), patch("autograb.phase2_cli.setup_email", new_callable=AsyncMock,
                                                              return_value=SetupResult("CONFIGURED")) as setup:
            code, _ = await self.run_cli("configure")
        self.assertEqual(code, 0)
        setup.assert_awaited_once_with(self.root)
        self.startPatch.assert_not_called()

    async def test_configure_menu_routes_human_session_command(self):
        with patch("builtins.input", return_value="2"), patch("autograb.main.run", new_callable=AsyncMock, return_value=0) as run:
            code, _ = await self.run_cli("configure")
        self.assertEqual(code, 0)
        self.assertEqual(run.await_args.args[0].command, "open-session")

    async def test_reconcile_unknown_and_incomplete_results_are_not_success(self):
        cases = [
            [{"status": "RECONCILIATION_REQUIRED", "error_code": "RECONCILIATION_REQUIRED"}],
            [{"status": "ORDER_CREATED", "error_code": "LOGIN_REQUIRED"}],
            [{"status": "INVOICE_NOT_FOUND", "error_code": "INVOICE_NOT_FOUND"}],
            [{"status": "PAYMENT_URL_READY", "error_code": "PAYMENT_PAGE_UNVERIFIED"}],
            [{"status": "WAITING_FOR_USER", "notification": {"status": "NOTIFICATION_FAILED"}}],
        ]
        for results in cases:
            with self.subTest(results=results), patch("autograb.core.purchase.PurchaseRunner") as runner:
                runner.return_value.recover = AsyncMock(return_value=results)
                code, output = await self.run_cli("reconcile")
                self.assertEqual(code, 2)
                self.assertIn('"status": "RECONCILIATION_REQUIRED"', output)
                self.assertIn('"automatic_resubmission": "DISABLED"', output)
        self.provider.submit_order.assert_not_awaited()

    async def test_reconcile_empty_resolved_and_confirmed_absent_succeed_without_retry(self):
        cases = [[], [{"status": "WAITING_FOR_USER", "notification": {"status": "SMTP_ACCEPTED"}}],
                 [{"status": "ORDER_SUBMIT_FAILED", "error_code": "ORDER_ABSENCE_CONFIRMED_NO_AUTORETRY"}]]
        for results in cases:
            with self.subTest(results=results), patch("autograb.core.purchase.PurchaseRunner") as runner:
                runner.return_value.recover = AsyncMock(return_value=results)
                code, output = await self.run_cli("reconcile")
                self.assertEqual(code, 0)
                self.assertIn('"status": "RECONCILIATION_COMPLETE"', output)
        self.provider.submit_order.assert_not_awaited()

    async def mock_armed_queue(self, results, *, hold_for_user=False):
        """Enter the otherwise-blocked loop only with explicit mocked preflight."""
        self.notifier.configured = True
        events = [{"id": f"fixture-event-{number}", "event_type": "NEW_PRODUCT",
                   "product": {"eligible": True}} for number in range(3)]
        guard = LiveGuard(self.root / "data", mode="LIVE")
        mock_preflight = Preflight({name: "PASS" for name in REQUIRED_CHECKS},
                                   SMTPProof("SMTP_ACCEPTED", "REAL_SMTP", datetime.now(timezone.utc)))
        held_checks = []
        if hold_for_user:
            self.browser.context = MagicMock(pages=["fixture-page"])

        async def release_held_browser(_):
            held_checks.append((guard.status()["armed"], self.browser.close.await_count))
            self.browser.context.pages = []

        with patch("autograb.phase2_cli.LiveGuard", return_value=guard), \
                patch("autograb.phase2_cli.collect_preflight", new_callable=AsyncMock, return_value=mock_preflight), \
                patch.object(Store, "ingest", return_value={"events": events}), \
                patch("autograb.core.purchase.PurchaseRunner") as runner, \
                patch("autograb.phase2_cli.monitoring_wait", new_callable=AsyncMock, return_value=False) as wait, \
                patch("autograb.phase2_cli.asyncio.sleep", new_callable=AsyncMock, side_effect=release_held_browser):
            runner.return_value.process = AsyncMock(side_effect=results)
            code, output = await self.run_cli("arm", "--mode", "LIVE")
        return code, output, runner.return_value.process, guard, held_checks, wait

    async def test_attention_result_stops_remaining_queue_and_disarms(self):
        statuses = ("LOGIN_REQUIRED", "SESSION_EXPIRED", "CAPTCHA_REQUIRED", "RECONCILIATION_REQUIRED",
                    "INVOICE_NOT_FOUND", "PAYMENT_URL_NOT_FOUND", "PAYMENT_URL_READY", "ORDER_CREATED",
                    "ORDER_ALREADY_EXISTS", "ORDER_SUBMITTING", "ORDER_SUBMIT_FAILED", "FAILED")
        for status in statuses:
            with self.subTest(status=status):
                code, output, process, guard, _, wait = await self.mock_armed_queue([{"status": status}])
                self.assertEqual(code, 2)
                self.assertEqual(process.await_count, 1)
                self.assertEqual(process.await_args.args[0]["id"], "fixture-event-0")
                self.assertFalse(guard.status()["armed"])
                self.assertIn('"remaining_queue": "NOT_DISPATCHED"', output)
                wait.assert_not_awaited()

    async def test_captcha_holds_browser_after_disarm_without_retry_or_next_dispatch(self):
        for result in ({"status": "CAPTCHA_REQUIRED"},
                       {"status": "RECONCILIATION_REQUIRED", "error_code": "CAPTCHA_REQUIRED"}):
            with self.subTest(result=result):
                self.browser.close.reset_mock()
                code, output, process, guard, held, wait = await self.mock_armed_queue([result], hold_for_user=True)
                self.assertEqual(code, 2)
                self.assertEqual(process.await_count, 1)
                self.assertEqual(held, [(False, 0)])
                self.browser.close.assert_awaited_once()
                self.assertIn("browser remains open", output)
                wait.assert_not_awaited()

    async def test_ready_result_disarms_and_waits_for_user_without_next_event(self):
        for status in ("PAYMENT_READY", "WAITING_FOR_USER"):
            with self.subTest(status=status):
                self.browser.close.reset_mock()
                code, output, process, guard, held, wait = await self.mock_armed_queue(
                    [{"status": status, "notification": {"status": "SMTP_ACCEPTED"}}], hold_for_user=True)
                self.assertEqual(code, 0)
                self.assertEqual(process.await_count, 1)
                self.assertFalse(guard.status()["armed"])
                self.assertEqual(held, [(False, 0)])
                self.assertIn("existing order retained", output)
                wait.assert_not_awaited()

    async def test_ready_but_failed_email_stops_queue_and_reports_attention(self):
        code, _, process, guard, _, _ = await self.mock_armed_queue(
            [{"status": "WAITING_FOR_USER", "notification": {"status": "NOTIFICATION_FAILED"}}])
        self.assertEqual(code, 2)
        self.assertEqual(process.await_count, 1)
        self.assertFalse(guard.status()["armed"])

    async def test_only_stale_or_already_claimed_events_may_be_skipped(self):
        code, output, process, guard, _, wait = await self.mock_armed_queue(
            [{"status": "STALE_EVENT"}, {"status": "ALREADY_CLAIMED"}, {"status": "LOGIN_REQUIRED"}])
        self.assertEqual(code, 2)
        self.assertEqual(process.await_count, 3)
        self.assertEqual([call.args[0]["id"] for call in process.await_args_list],
                         ["fixture-event-0", "fixture-event-1", "fixture-event-2"])
        self.assertFalse(guard.status()["armed"])
        wait.assert_not_awaited()

    async def test_invalid_purchase_result_stops_and_disarms(self):
        for result in (None, {}, {"status": []}, {"status": "UNRECOGNIZED"}):
            with self.subTest(result=result):
                code, output, process, guard, _, _ = await self.mock_armed_queue([result])
                self.assertEqual(code, 2)
                self.assertEqual(process.await_count, 1)
                self.assertIn("UNVERIFIED_PURCHASE_RESULT", output)
                self.assertFalse(guard.status()["armed"])


class Phase2EventLogTests(unittest.TestCase):
    def test_live_label_is_correct_and_write_cannot_override_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = EventLog(path, mode="LIVE")
            with redirect_stdout(io.StringIO()):
                log.write("FIXTURE_LIVE_EVENT")
            self.assertEqual(json.loads(path.read_text())["mode"], "LIVE")
            with self.assertRaises(ValueError):
                log.write("FIXTURE_LIVE_EVENT", mode="DRY_RUN")

    def test_invalid_mode_construction_is_rejected(self):
        with self.assertRaises(ValueError):
            EventLog(Path("unused.jsonl"), mode="UNRECOGNIZED")


if __name__ == "__main__":
    unittest.main()
