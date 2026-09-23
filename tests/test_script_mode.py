"""Cron-facing contracts, without merchant requests or real notification sends."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from autograb import run_provider
from autograb.core.config import Config
from autograb.core.errors import AutoGrabError
from autograb.core.http_metrics import request_started, RequestMeter, current_meter
from autograb.core.models import Product
from autograb.core.rate_budget import ProviderRateBudget
from autograb.core.scheduled import wait_until, launch_timestamp
from autograb.core.snapshot import ScanState, save_projection
from autograb.multi_cli import process_opportunity
from autograb.notifications.adapter import NotificationAdapter, notification_url
from autograb.notifications.email import EmailNotifier, SMTPConfig, NotificationResult
from autograb.providers.vmiss import VMISSProvider, CATALOG_URL
from autograb.script_cli import check_once, configured, execute, parser
from autograb.storage.database import Store

ROOT = Path(__file__).resolve().parents[1]


def product():
    return Product("87", "Regular annual", "AVAILABLE",
        [{"period": "annually", "cents": 4999, "currency": "USD"}],
        "https://bandwagonhost.com/order/ecommerce", provider="bandwagon")


def filesystem(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes())
            for p in root.rglob("*") if p.is_file()}


class ScriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(root=self.root)
        self.config.prepare()
        self.store = Store(self.root / "data/autograb.sqlite3")
        self.addCleanup(self.store.close)
        self.args = parser().parse_args(["bandwagon", "--root", str(self.root), "--json"])
        async def discover():
            request_started()
            return [product()]
        self.provider = SimpleNamespace(provider_name="bandwagon", discover_products=AsyncMock(side_effect=discover))

    async def check(self):
        with patch("autograb.script_cli.create_provider", return_value=self.provider):
            return await check_once("bandwagon", self.config, self.store, self.args)

    async def test_baseline_silent_then_restart_budget_zero_requests(self):
        with patch.object(NotificationAdapter, "send_event", new_callable=AsyncMock) as send:
            first = await self.check()
            second = await self.check()
        self.assertEqual((first["status"], first["request_count"], first["opportunities"]), ("BASELINE", 1, 0))
        self.assertEqual((second["status"], second["request_count"]), ("WAITING", 0))
        self.assertEqual(self.provider.discover_products.await_count, 1)
        send.assert_not_awaited()

    async def test_public_denial_retry_after_persists_and_burst_cannot_override(self):
        self.provider.discover_products.side_effect = AutoGrabError("RATE_LIMITED")
        self.provider.last_retry_after = "7200"
        first = await self.check()
        self.args.pace = "BURST"
        second = await self.check()
        self.assertEqual(first["status"], "BLOCKED")
        self.assertGreater(first["rate_budget"]["wait_seconds"], 7190)
        self.assertEqual(second["request_count"], 0)
        self.assertEqual(self.provider.discover_products.await_count, 1)

    async def test_query_does_not_touch_database_sidecars_logs_or_projection(self):
        self.store.ingest([product()])
        save_projection(self.root, self.store, "bandwagon", {"status": "MONITORED"})
        before = filesystem(self.root)
        self.args.mode = "QUERY"
        out = StringIO()
        with redirect_stdout(out), patch("autograb.script_cli.Store", side_effect=AssertionError("QUERY opened SQLite")):
            self.assertEqual(await execute(self.args, self.config), 0)
        self.assertEqual(filesystem(self.root), before)
        result = json.loads(out.getvalue())
        self.assertFalse(result["fresh"])
        self.assertEqual(result["request_count"], 0)
        self.assertEqual(len(result["providers"][0]["products"]), 1)

    async def test_no_opportunity_run_is_quiet_and_exit_zero(self):
        self.args.json = False
        out = StringIO()
        with redirect_stdout(out), patch("autograb.script_cli.create_provider", return_value=self.provider):
            self.assertEqual(await execute(self.args, self.config), 0)
        self.assertEqual(out.getvalue(), "")

    async def test_notify_precedes_edge_and_is_claimed_once(self):
        self.store.ingest([replace(product(), availability="SOLD_OUT")])
        event = self.store.ingest([product()])["events"][0]
        calls = []
        async def send(*_):
            calls.append("email")
            return NotificationResult("SMTP_ACCEPTED")
        async def check(*_):
            calls.append("edge_precheck")
            raise AutoGrabError("STOCK_UNKNOWN")
        provider = SimpleNamespace(provider_name="bandwagon", check_product=check)
        notifier = SimpleNamespace(send_event=send)
        for _ in range(2):
            await process_opportunity(self.store, provider, event, notifier, prepare_checkout=True, notify_first=True)
        self.assertEqual(calls, ["email", "edge_precheck"])
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM notifications").fetchone()[0], 1)

    async def test_failed_notification_never_dispatches_edge(self):
        self.store.ingest([replace(product(), availability="SOLD_OUT")])
        event = self.store.ingest([product()])["events"][0]
        provider = SimpleNamespace(provider_name="bandwagon", check_product=AsyncMock())
        notifier = SimpleNamespace(send_event=AsyncMock(return_value=NotificationResult("NOTIFICATION_FAILED")))
        result = await process_opportunity(self.store, provider, event, notifier, prepare_checkout=True, notify_first=True)
        self.assertEqual(result, "NOTIFICATION_FAILED")
        provider.check_product.assert_not_awaited()

    async def test_vmiss_scan_resumes_in_new_provider_without_partial_baseline(self):
        now = [1000.0]
        budget = ProviderRateBudget(self.store, clock=lambda: now[0])
        state = ScanState(self.store, "vmiss:catalog")
        first = VMISSProvider(budget=budget, scan_state=state)
        group = CATALOG_URL + "/test-group"
        vm_product = replace(product(), provider="vmiss", product_id="test-group/test-plan",
                             product_url=group, order_url=group + "/test-plan")
        with patch.object(first, "_http", return_value="fixture"), patch("autograb.providers.vmiss._parse_page", return_value=([], [group])):
            with self.assertRaisesRegex(AutoGrabError, "CATALOG_INCOMPLETE"):
                await first.discover_products()
        self.assertEqual(self.store.summary()["known_count"], 0)
        now[0] += 901
        second = VMISSProvider(budget=budget, scan_state=ScanState(self.store, "vmiss:catalog"))
        with patch.object(second, "_http", return_value="fixture") as request, patch("autograb.providers.vmiss._parse_page", return_value=([vm_product], [])):
            self.assertEqual(await second.discover_products(), [vm_product])
        request.assert_called_once_with(group)
        self.assertIsNone(state.load())

    async def test_vmiss_cooldown_takes_precedence_over_saved_scan(self):
        budget = ProviderRateBudget(self.store)
        ticket = budget.claim("vmiss", "global", "catalog", interval_seconds=900)
        budget.failure(ticket, "RATE_LIMITED", retry_after="3600", limited=True)
        provider = VMISSProvider(budget=budget, scan_state=ScanState(self.store, "vmiss:catalog"))
        with patch.object(provider, "_http", side_effect=AssertionError("HTTP during cooldown")):
            with self.assertRaisesRegex(AutoGrabError, "RATE_LIMIT_WAIT"):
                await provider.discover_products()

    async def test_precise_wait_sleeps_warms_once_and_never_returns_early(self):
        now = [1000.0]
        delays, warmed = [], []
        async def sleep(seconds):
            self.assertGreater(seconds, 0)
            delays.append(seconds)
            now[0] += seconds
        async def warm():
            warmed.append(now[0])
        await wait_until(1120, data_dir=self.root / "data", warm=warm, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(warmed, [1090])
        self.assertGreaterEqual(now[0], 1120)
        self.assertLess(len(delays), 30)
        self.assertLessEqual(max(delays), 30)

    async def test_wait_respects_stop_before_any_work(self):
        (self.root / "data/stop-monitoring.signal").touch()
        with self.assertRaisesRegex(AutoGrabError, "MONITORING_STOPPED"):
            await wait_until(9999999999, data_dir=self.root / "data", sleep=AsyncMock())

    async def test_expired_burst_does_not_start_even_within_late_grace(self):
        self.args.launch_at = "2026-11-27T00:00:00+00:00"
        self.args.pace, self.args.burst_seconds = "BURST", 1
        with patch("autograb.script_cli.time.time", return_value=launch_timestamp(self.args.launch_at) + 2):
            with self.assertRaisesRegex(AutoGrabError, "LAUNCH_WINDOW_ENDED"):
                await execute(self.args, self.config)

    async def test_apple_rotation_survives_script_process_lifetime(self):
        self.config.providers["apple"] = {"region": "cn", "targets": [{"sku": "MYEV3CH/A", "stores": ["R359", "R401"], "modes": ["pickup"]}]}
        calls = []
        def transport(url):
            calls.append(url)
            return 541, None, "900"
        with patch("autograb.providers.apple._http_get", side_effect=transport):
            one = await check_once("apple", self.config, self.store, self.args)
            two = await check_once("apple", self.config, self.store, self.args)
        self.assertEqual(one["reason"], "HTTP_BLOCKED")
        self.assertEqual(two["reason"], "RATE_LIMIT_WAIT")
        self.assertEqual(len(calls), 1)
        self.assertEqual(ScanState(self.store, "apple:cn:pickup_cursor").load()["next"], 1)


class ScriptBoundaryTests(unittest.TestCase):
    def test_burst_deadline_blocks_later_pages_before_http_dispatch(self):
        meter = RequestMeter(deadline=10)
        token = current_meter.set(meter)
        try:
            with patch("autograb.core.http_metrics.time.time", return_value=10):
                with self.assertRaisesRegex(AutoGrabError, "LAUNCH_WINDOW_ENDED"):
                    request_started()
            self.assertEqual(meter.count, 0)
        finally:
            current_meter.reset(token)

    def test_qinglong_rejects_dry_run_and_edge(self):
        for options in (["--mode", "DRY_RUN"], ["--prepare-checkout"]):
            with self.assertRaisesRegex(AutoGrabError, "QINGLONG_MONITOR_ONLY"):
                configured(parser().parse_args(["bandwagon", *options]), qinglong=True)

    def test_invalid_env_and_burst_fail_closed(self):
        for key, value in (("AUTOGRAB_MODE", "LIVE"), ("AUTOGRAB_DMIT_ENABLED", "yes")):
            with patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    configured(parser().parse_args(["dmit"]))
        with self.assertRaises(ValueError):
            configured(parser().parse_args(["dmit", "--pace", "BURST"]))
        with self.assertRaises(ValueError):
            launch_timestamp("2026-11-27T09:00:00")

    def test_thin_wrappers_run_without_optional_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "PYTHONPATH": str(ROOT), "AUTOGRAB_ROOT": directory, "AUTOGRAB_MODE": "QUERY"}
            for name in ("bandwagon", "dmit", "vmiss", "vps", "apple"):
                result = subprocess.run([sys.executable, "-S", str(ROOT / f"scripts/qinglong/autograb_{name}.py")],
                    env=env, cwd=directory, text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["request_count"], 0)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_new_email_alias_and_simple_facade_use_existing_smtp(self):
        config = SMTPConfig.from_env({"AUTOGRAB_EMAIL": "you@example.com", "AUTOGRAB_SMTP_HOST": "smtp.example.com",
            "AUTOGRAB_SMTP_USER": "you@example.com", "AUTOGRAB_SMTP_PASSWORD": "synthetic-test-only"})
        self.assertIsNone(config.error_code)
        email = EmailNotifier(config)
        with patch.object(email, "_send", return_value=NotificationResult("SMTP_ACCEPTED")) as send:
            result = asyncio.run(NotificationAdapter(email).notify("AutoGrab test", "No order", priority="normal"))
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        self.assertIn("AutoGrab test", send.call_args.args[0]["Subject"])

    def test_notification_urls_accept_five_official_sources_and_reject_secrets(self):
        for url in ("https://bandwagonhost.com/order/ecommerce", "https://www.dmit.io/cart.php?gid=1",
                    "https://app.vmiss.com/store/us-los-angeles-cmin2", "https://v.ps/products/mini-vps",
                    "https://www.apple.com.cn/shop"):
            self.assertEqual(notification_url(url), url)
        for url in ("https://example.test/invoice", "https://www.apple.com.cn/shop?token=secret", "http://localhost:8000", "UNAVAILABLE"):
            self.assertIsNone(notification_url(url))

    def test_disabled_provider_is_no_network_and_quiet(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AUTOGRAB_BANDWAGON_ENABLED": "false"}), redirect_stdout(StringIO()) as output:
            with patch("autograb.script_cli.create_provider", side_effect=AssertionError("disabled provider")):
                self.assertEqual(run_provider("bandwagon", root=directory), 0)
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
