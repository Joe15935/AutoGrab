"""Normal challenge request types render in a wholly offline Chromium fixture.

All requests pass through the production policy and then fixture fulfillment or
abort. No route in this file calls Playwright route.continue_ or accesses a real
account, CAPTCHA or challenge service. These fixtures do not solve challenges.
"""

from pathlib import Path
import tempfile
import unittest

from playwright.async_api import async_playwright

from autograb.browser.login_policy import HumanLoginPolicy
from autograb.browser.manager import BrowserManager
from autograb.browser.safety import SafetyPolicy
from autograb.core.config import Config
from autograb.core.errors import AutoGrabError


HOST = "https://bandwagonhost.com"
CF = "https://challenges.cloudflare.com"
PAGE = HOST + "/clientarea.php?__cf_chl_rt_tk=fixture-navigation"
SCRIPT = HOST + "/cdn-cgi/challenge-platform/h/g/orchestrate/fixture"
FLOW = HOST + "/cdn-cgi/challenge-platform/h/g/flow/fixture"
FRAME = CF + "/cdn-cgi/challenge-platform/h/g/frame/fixture"
LOADER = CF + "/turnstile/v0/api.js"


class _FixtureRoute:
    def __init__(self, route, harness):
        self.request = route.request
        self._route, self._harness = route, harness

    async def continue_(self):
        self._harness.admitted.append((self.request.method, self.request.url))
        response = self._harness.responses.get(self.request.url)
        if response is None:
            await self._route.abort("failed")
            return
        status, content_type, body = response
        await self._route.fulfill(status=status, content_type=content_type, body=body)

    async def abort(self, reason):
        await self._route.abort(reason)


class HumanLoginOfflineBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(root=self.root, timeout_ms=1500)
        self.config.prepare()
        self.browser = BrowserManager(self.config)
        self.browser.policy = HumanLoginPolicy()
        self.browser.playwright = await async_playwright().start()
        self.browser.context = await self.browser.playwright.chromium.launch_persistent_context(
            str(self.root / "profiles/bandwagon"), headless=True, service_workers="block",
        )
        self.admitted = []
        self.responses = {
            PAGE: (200, "text/html", f'''<title>Offline login fixture</title>
                <h1>Sign in</h1><p id="loader">pending</p><p id="flow">pending</p>
                <script src="{LOADER}"></script><script src="{SCRIPT}"></script>
                <iframe title="Offline verification frame" src="{FRAME}"></iframe>'''),
            LOADER: (200, "application/javascript", 'document.querySelector("#loader").textContent = "fixture script loaded";'),
            SCRIPT: (200, "application/javascript", f'''fetch("{FLOW}", {{method:"POST", body:"fixture-only"}})
                .then(r=>r.text()).then(t=>document.querySelector("#flow").textContent=t)
                .catch(()=>document.querySelector("#flow").textContent="blocked");'''),
            FLOW: (200, "text/plain", "fixture POST accepted"),
            FRAME: (200, "text/html", "<p>Offline verification frame rendered</p>"),
        }

        async def route(request_route):
            await self.browser.policy.route(_FixtureRoute(request_route, self))

        await self.browser.context.route("**/*", route)
        self.browser.context.set_default_timeout(1500)
        self.browser.page = await self.browser.context.new_page()
        self.browser.catalog_page = await self.browser.context.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        self.temporary.cleanup()

    async def test_manual_fixture_renders_loader_challenge_post_and_subframe(self):
        await self.browser.navigate(self.browser.page, PAGE)
        await self.browser.page.get_by_text("fixture POST accepted", exact=True).wait_for()
        self.assertEqual(await self.browser.page.locator("#loader").inner_text(), "fixture script loaded")
        frame_text = await self.browser.page.frame_locator('iframe').locator("p").inner_text()
        self.assertEqual(frame_text, "Offline verification frame rendered")
        self.assertIn(("GET", PAGE), self.admitted)
        self.assertIn(("GET", LOADER), self.admitted)
        self.assertIn(("GET", SCRIPT), self.admitted)
        self.assertIn(("POST", FLOW), self.admitted)
        self.assertIn(("GET", FRAME), self.admitted)
        self.assertTrue(all(url in self.responses for _, url in self.admitted))

    async def test_versioned_turnstile_fixture_script_is_loaded_and_renders(self):
        versioned = CF + "/turnstile/v0/g/abc012345678/api.js?render=explicit"
        status, content_type, body = self.responses[PAGE]
        self.responses[PAGE] = (status, content_type, body.replace(LOADER, versioned))
        self.responses[versioned] = self.responses.pop(LOADER)
        await self.browser.navigate(self.browser.page, PAGE)
        await self.browser.page.get_by_text("fixture POST accepted", exact=True).wait_for()
        self.assertEqual(await self.browser.page.locator("#loader").inner_text(), "fixture script loaded")
        self.assertIn(("GET", versioned), self.admitted)
        self.assertNotIn(("GET", LOADER), self.admitted)
        self.assertTrue(all(url in self.responses for _, url in self.admitted))

    async def test_real_browser_dispatch_still_blocks_order_payment_and_nonchallenge_post(self):
        await self.browser.navigate(self.browser.page, PAGE)
        await self.browser.page.get_by_text("fixture POST accepted", exact=True).wait_for()
        before = list(self.admitted)
        paths = [
            "/cart.php?a=checkout", "/cart.php?a=complete", "/viewinvoice.php?id=1",
            "/creditcard.php", "/paypal.php", "/clientarea.php?action=addfunds",
            "/cdn-cgi/not-challenge/flow",
        ]
        outcomes = await self.browser.page.evaluate('''async paths => Promise.all(paths.map(async path => {
            try { await fetch(path, {method:'POST', body:'synthetic-never-dispatched'}); return 'unexpected'; }
            catch (_) { return 'blocked'; }
        }))''', paths)
        self.assertEqual(outcomes, ["blocked"] * len(paths))
        self.assertEqual(self.admitted, before)
        self.assertIn({"method": "POST", "path": "/cart.php", "resource_type": "fetch"}, self.browser.policy.blocked)

    async def test_title_only_challenge_retains_page_for_human_without_retry(self):
        self.responses[PAGE] = (403, "text/html", "<title>Just a moment...</title><body></body>")
        with self.assertRaises(AutoGrabError) as raised:
            await self.browser.navigate(self.browser.page, PAGE)
        self.assertEqual(raised.exception.code, "CAPTCHA_REQUIRED")
        self.assertFalse(self.browser.page.is_closed())
        self.assertEqual(self.browser.page.url, PAGE)
        self.assertEqual(self.admitted.count(("GET", PAGE)), 1)

    async def test_default_monitor_policy_keeps_challenge_post_blocked(self):
        self.browser.policy = SafetyPolicy()
        public = HOST + "/order/basic"
        self.responses[public] = (200, "text/html", "<h1>Public offline fixture</h1>")
        await self.browser.navigate(self.browser.page, public)
        result = await self.browser.page.evaluate('''async url => {
            try { await fetch(url, {method:'POST', body:'fixture-only'}); return 'unexpected'; }
            catch (_) { return 'blocked'; }
        }''', FLOW)
        self.assertEqual(result, "blocked")
        self.assertEqual(self.admitted, [("GET", public)])


if __name__ == "__main__":
    unittest.main()
