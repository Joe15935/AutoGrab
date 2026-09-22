"""Public route and installer boundaries; never launch Edge or touch real profiles."""
from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from autograb.core.config import Config
from autograb.core.models import Product
from autograb.edge_cli import handles, product_payload, run_edge
from autograb.edge_install import identity, install, open_edge, registration_path
from autograb.main import main, parser
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import make_message

ROOT = Path(__file__).resolve().parents[1]


class EdgeEntrypointTests(unittest.TestCase):
    def test_authenticated_commands_route_to_edge(self):
        for command in ("open-session", "configure", "preflight", "arm", "reconcile", "stop-all"):
            args = Namespace(command=command, offline=False)
            self.assertTrue(handles(args))
        self.assertFalse(handles(Namespace(command="preflight", offline=True)))
        for command in ("baseline", "monitor", "dry-run", "probe"):
            self.assertFalse(handles(Namespace(command=command)))

    def test_real_public_main_does_not_enter_playwright_login(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["autograb", "--root", directory, "open-session"]), \
                    patch("autograb.edge_cli.run_edge", new_callable=AsyncMock, return_value=0) as edge, \
                    patch("autograb.main.run", new_callable=AsyncMock) as legacy:
                self.assertEqual(main(), 0)
                edge.assert_awaited_once()
                legacy.assert_not_called()

    def test_real_public_main_disables_old_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["autograb", "--root", directory, "arm", "--mode", "LIVE", "--hours", "1"]), \
                    patch("autograb.edge_cli.run_edge", new_callable=AsyncMock, return_value=2) as edge, \
                    patch("autograb.phase2_cli.run_phase2", new_callable=AsyncMock) as legacy:
                self.assertEqual(main(), 2)
                edge.assert_awaited_once()
                legacy.assert_not_called()

    def test_new_cli_requires_identity(self):
        with redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            for command in ("edge-dry-run", "edge-resume", "edge-cancel"):
                with self.assertRaises(SystemExit):
                    parser().parse_args([command])

    def test_launch_services_has_no_browser_flags(self):
        with patch("autograb.edge_install.subprocess.run") as call:
            open_edge("https://bandwagonhost.com/clientarea.php")
            call.assert_called_once_with(["/usr/bin/open", "-a", "Microsoft Edge", "https://bandwagonhost.com/clientarea.php"], check=True)

    def test_product_payload_has_no_account_or_order_fields(self):
        product = Product("87", "Synthetic", "AVAILABLE", [{"cents": 4999, "currency": "USD", "period": "Annually"}],
                          "https://bandwagonhost.com/order/ecommerce/test")
        result = product_payload(product)
        self.assertEqual(result["mode"], "DRY_RUN")
        self.assertEqual(set(result["product"]), {"name", "url", "period", "cents", "currency"})


class EdgeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project with spaces"
        self.home = Path(self.temp.name) / "user"
        (self.root / "edge-extension").mkdir(parents=True)
        public_identity = json.loads((ROOT / "config/edge-identity.json").read_text())
        (self.root / "edge-extension/manifest.json").write_text(json.dumps({"key": public_identity["key"]}))
        (self.root / ".venv/bin").mkdir(parents=True)
        python = self.root / ".venv/bin/python"
        python.write_text("#!/bin/sh\nexit 0\n")
        python.chmod(0o700)

    def test_registration_exact_origin_private_permissions_and_no_install_claim(self):
        result = install(self.root, home=self.home, open_ui=False)
        target = registration_path(self.home)
        manifest = json.loads(target.read_text())
        self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{identity(self.root)}/"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        launcher = Path(manifest["path"])
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)
        self.assertIn('"$@"', launcher.read_text())
        self.assertIn("AWAITING_BROWSER", result["installed"])
        self.assertFalse((self.home / "Library/Application Support/Microsoft Edge/Default").exists())

    def test_reinstall_same_project_idempotent(self):
        first = install(self.root, home=self.home, open_ui=False)
        self.assertEqual(install(self.root, home=self.home, open_ui=False), first)

    def test_conflicting_registration_preserved(self):
        target = registration_path(self.home)
        target.parent.mkdir(parents=True)
        original = '{"name":"other"}'
        target.write_text(original)
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            install(self.root, home=self.home, open_ui=False)
        self.assertEqual(target.read_text(), original)

    def test_conflicting_launcher_preserved(self):
        folder = self.home / "Applications/AutoGrab Edge Companion"
        folder.mkdir(parents=True)
        (folder / "native-host").write_text("user file")
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            install(self.root, home=self.home, open_ui=False)
        self.assertEqual((folder / "native-host").read_text(), "user file")

    def test_install_opens_only_extensions_and_project_folder(self):
        with patch("autograb.edge_install.subprocess.run") as run:
            install(self.root, home=self.home)
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/open", "-a", "Microsoft Edge", "edge://extensions"])
        self.assertEqual(run.call_args_list[1].args[0], ["/usr/bin/open", str(self.root.resolve() / "edge-extension")])


class EdgeLiveDisabledTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_dry_run_opens_edge_without_creating_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(root=Path(directory)); config.prepare()
            product = Product("87", "Synthetic", "AVAILABLE", [{"cents": 4999, "currency": "USD", "period": "Annually"}],
                              "https://bandwagonhost.com/order/ecommerce/Fixture/TEST")
            with Store(config.root / "data/autograb.sqlite3") as store:
                store.ingest([product])
            with patch("autograb.edge_cli.open_edge") as opened, redirect_stdout(io.StringIO()):
                self.assertEqual(await run_edge(Namespace(command="edge-dry-run", product_id="87", wait_seconds=0), config), 2)
                opened.assert_called_once_with(product.product_url)
            with Store(config.root / "data/autograb.sqlite3") as store:
                self.assertEqual(IntentStore(store).list(), [])
                self.assertEqual(store.summary()["event_count"], 0)

    async def test_connected_explicit_dry_run_creates_only_one_persistent_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(root=Path(directory)); config.prepare()
            product = Product("87", "Synthetic", "AVAILABLE", [{"cents": 4999, "currency": "USD", "period": "Annually"}],
                              "https://bandwagonhost.com/order/ecommerce/Fixture/TEST")
            with Store(config.root / "data/autograb.sqlite3") as store:
                store.ingest([product])
                EdgeBroker(store).handle_event(make_message("EDGE_READY", payload={"version": "0.2.1"}))
            with patch("autograb.edge_cli.open_edge") as opened, redirect_stdout(io.StringIO()):
                for _ in range(2):
                    self.assertEqual(await run_edge(Namespace(command="edge-dry-run", product_id="87", wait_seconds=0), config), 2)
                opened.assert_not_called()
            with Store(config.root / "data/autograb.sqlite3") as store:
                intents = IntentStore(store).list()
                self.assertEqual(len(intents), 1)
                self.assertIsNone(intents[0]["submit_started_at"])
                self.assertEqual(store.summary()["known_count"], 1)
                self.assertEqual(store.summary()["event_count"], 1)

    async def test_preflight_arm_reconcile_never_start_old_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(root=Path(directory))
            config.prepare()
            with patch("autograb.main.BrowserManager") as browser, redirect_stdout(io.StringIO()):
                for command in ("preflight", "arm", "reconcile"):
                    self.assertEqual(await run_edge(Namespace(command=command), config), 2)
                browser.assert_not_called()
