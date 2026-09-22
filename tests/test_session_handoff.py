"""Manual login handoff lifecycle; no browser, network, or Keychain access."""

import argparse
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from autograb.core.config import Config
from autograb.core.errors import AutoGrabError
from autograb.main import run
from autograb.notifications.email import SMTPConfig
from autograb.storage.database import Store


class SessionHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(root=self.root)
        self.config.prepare()
        self.lifecycle = []
        self.browser = MagicMock()
        self.browser.policy = SimpleNamespace(login_mode=False)
        self.browser.start = AsyncMock()
        self.browser.navigate = AsyncMock()
        self.browser.artifact = AsyncMock(return_value={"artifact_status": "MOCK"})

        async def close():
            self.lifecycle.append("close")

        self.browser.close = AsyncMock(side_effect=close)
        self.provider = MagicMock(routes={})
        self.provider.discover_products = AsyncMock()
        self.log = MagicMock()

        async def hold(browser, log, reason):
            self.assertIs(browser, self.browser)
            self.assertIs(log, self.log)
            self.browser.close.assert_not_awaited()
            self.lifecycle.append("hold:" + reason)

        self.hold = AsyncMock(side_effect=hold)
        self.enterContext(patch("autograb.main.BrowserManager", return_value=self.browser))
        self.enterContext(patch("autograb.main.BandwagonHostProvider", return_value=self.provider))
        self.enterContext(patch("autograb.main.EventLog", return_value=self.log))
        self.enterContext(patch("autograb.main.EmailNotifier", return_value=MagicMock(configured=False)))
        self.enterContext(patch("autograb.main.load_setup", return_value=SMTPConfig.from_env({})))
        self.enterContext(patch("autograb.main.keep_open", self.hold))

    def saved_status(self):
        with Store(self.root / "data/autograb.sqlite3") as store:
            rows = store.connection.execute("SELECT status FROM runs").fetchall()
        self.assertEqual(len(rows), 1)
        return rows[0]["status"]

    async def test_open_session_http_403_holds_before_close_without_retry(self):
        self.browser.navigate.side_effect = AutoGrabError("HTTP_403")

        result = await run(argparse.Namespace(command="open-session"), self.config)

        self.assertEqual(result, 1)
        self.assertTrue(self.browser.policy.login_mode)
        self.browser.start.assert_awaited_once()
        self.browser.navigate.assert_awaited_once_with(
            self.browser.page, "https://bandwagonhost.com/clientarea.php")
        self.hold.assert_awaited_once_with(self.browser, self.log, "HTTP_403")
        self.browser.close.assert_awaited_once()
        self.provider.discover_products.assert_not_awaited()
        self.assertEqual(self.lifecycle, ["hold:HTTP_403", "close"])
        self.assertEqual(self.saved_status(), "FAILED")

    async def test_probe_http_403_ends_without_manual_login_handoff_or_retry(self):
        self.provider.discover_products.side_effect = AutoGrabError("HTTP_403")

        result = await run(argparse.Namespace(command="probe"), self.config)

        self.assertEqual(result, 1)
        self.assertFalse(self.browser.policy.login_mode)
        self.provider.discover_products.assert_awaited_once()
        self.browser.navigate.assert_not_awaited()
        self.hold.assert_not_awaited()
        self.browser.close.assert_awaited_once()
        self.assertEqual(self.lifecycle, ["close"])
        self.assertEqual(self.saved_status(), "FAILED")

    async def test_normal_open_session_preserves_existing_manual_handoff(self):
        result = await run(argparse.Namespace(command="open-session"), self.config)

        self.assertEqual(result, 0)
        self.assertTrue(self.browser.policy.login_mode)
        self.browser.start.assert_awaited_once()
        self.browser.navigate.assert_awaited_once_with(
            self.browser.page, "https://bandwagonhost.com/clientarea.php")
        self.hold.assert_awaited_once_with(self.browser, self.log, "HUMAN_SESSION_LOGIN")
        self.browser.close.assert_awaited_once()
        self.provider.discover_products.assert_not_awaited()
        self.browser.artifact.assert_not_awaited()
        self.assertEqual(self.lifecycle, ["hold:HUMAN_SESSION_LOGIN", "close"])
        # Completion records the handoff ending; it does not assert authentication.
        self.assertEqual(self.saved_status(), "COMPLETE")


if __name__ == "__main__":
    unittest.main()
