"""Normal, headed Chromium; dedicated session; no protection bypass."""
import html
import json
import os
import re
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit, parse_qs

from autograb.core.errors import AutoGrabError
from autograb.core.lock import ProcessLock
from .safety import SafetyPolicy, public_url


class BrowserManager:
    def __init__(self, config):
        self.config = config
        self.policy = SafetyPolicy()
        self.context = self.playwright = self.page = self.catalog_page = None
        self._lock = None
        self.last_page = None

    async def start(self):
        if self.context:
            return
        self._lock = ProcessLock(self.config.root / "profiles/bandwagon/.autograb.lock")
        self._lock.__enter__()
        try:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.config.root / "profiles/bandwagon"), headless=False,
                service_workers="block", viewport={"width": 1280, "height": 900},
            )
            await self.context.route("**/*", self.policy.route)
            self.context.set_default_timeout(self.config.timeout_ms)
            self.catalog_page = await self.context.new_page()
            self.page = await self.context.new_page()
        except Exception:
            await self.close()
            raise AutoGrabError("BROWSER_START_FAILED") from None

    async def close(self):
        try:
            if self.context:
                await self.context.close()
            if self.playwright:
                await self.playwright.stop()
        finally:
            if self._lock:
                self._lock.__exit__()
            self.context = self.playwright = self._lock = None

    async def guard_page(self, page, status=None):
        self.last_page = page
        text = (await page.locator("body").inner_text())[:10000].lower()
        if self.policy.login_mode:
            # A partially loaded challenge may have an empty body but a title.
            text = (await page.title()).lower() + " " + text
        if any(s in text for s in ("verify you are human", "checking your browser", "just a moment", "captcha", "performing security verification")):
            raise AutoGrabError("CAPTCHA_REQUIRED")
        if "cloudflare" in text and (status == 403 or "access denied" in text):
            raise AutoGrabError("CLOUDFLARE")
        if status == 403:
            raise AutoGrabError("HTTP_403")
        if not self.policy.login_mode and urlsplit(page.url).path in {"/login.php", "/clientarea.php"}:
            raise AutoGrabError("LOGIN_REQUIRED")
        if status is not None and status >= 400:
            raise AutoGrabError("NETWORK_ERROR")

    async def navigate(self, page, url):
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        self.last_page = page
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
            await self.guard_page(page, response.status if response else None)
        except PlaywrightTimeout:
            raise AutoGrabError("TIMEOUT") from None
        except AutoGrabError:
            raise
        except Exception:
            raise AutoGrabError("NETWORK_ERROR") from None

    async def artifact(self, code):
        """Save only a DOM allowlist, never raw HTML / input values / links."""
        page = self.last_page or self.catalog_page
        directory = self.config.root / "artifacts" / str(uuid4())
        directory.mkdir(mode=0o700, parents=True)
        result = {"error_code": code, "directory": str(directory.relative_to(self.config.root)), "screenshot": "SUPPRESSED_PRIVATE_PAGE"}
        if page is None:
            return result
        try:
            # Text allowlist plus conservative suppression of any account page.
            info = await page.evaluate("""() => ({
                headings:[...document.querySelectorAll('h1,h2,h3')].map(e=>e.innerText).slice(0,25),
                buttons:[...document.querySelectorAll('button,input[type=submit]')].map(e=>e.innerText||e.value).slice(0,15),
                privatePage:!!document.querySelector('input[type=password],input[type=email],input[name=firstname],input[name=address1]') || /logout|log out|sign out/i.test(document.body.innerText)
            })""")
            safe = {"error_code": code, "path": public_url(page.url)}
            if not info["privatePage"]:
                safe.update({k: info[k] for k in ("headings", "buttons")})
                await page.screenshot(path=str(directory / "page.png"), full_page=False,
                                      mask=[page.locator("input,textarea,select,iframe")])
                result["screenshot"] = "MASKED_PUBLIC_PAGE"
            (directory / "page.sanitized.html").write_text("<meta charset=utf-8><pre>" + html.escape(json.dumps(safe, ensure_ascii=False, indent=2)) + "</pre>")
            (directory / "manifest.json").write_text(json.dumps(result, indent=2))
            for child in directory.iterdir():
                child.chmod(0o600)
        except Exception:
            result["artifact_status"] = "CAPTURE_FAILED"
        return result

    async def configuration_evidence(self, product):
        await self.guard_page(self.page)
        if await self.page.get_by_role("heading", name="Product Configuration", exact=True).count() != 1:
            raise AutoGrabError("SELECTOR_CHANGED")
        normalize = lambda s: re.sub(r"\s+", " ", s).strip().casefold()
        names = await self.page.locator("strong").all_text_contents()
        expected_names = {normalize(product.name), normalize("VPS - Self-managed - " + product.name)}
        if sum(normalize(name) in expected_names for name in names) != 1:
            raise AutoGrabError("PRODUCT_MISMATCH")
        select = self.page.locator('select[name="billingcycle"]')
        if await select.count() != 1:
            raise AutoGrabError("SELECTOR_CHANGED")
        selected = await select.locator("option:checked").inner_text()
        billing = await select.input_value()
        # Presence of both price and named billing, plus a real next-step control.
        if not re.search(r"\d+[.,]\d{2}", selected) or not re.search(r"monthly|quarterly|annually|biennially|triennially", selected, re.I):
            raise AutoGrabError("SELECTOR_CHANGED")
        next_button = self.page.get_by_role("button", name="Add to Cart", exact=True)
        if await next_button.count() != 1 or not await next_button.is_visible() or not await next_button.is_enabled():
            raise AutoGrabError("SELECTOR_CHANGED")
        forms = await self.page.locator("form").evaluate_all("fs=>fs.map(f=>({method:f.method,action:f.action}))")
        if not any(f["method"].lower() == "post" and urlsplit(f["action"]).path == "/cart.php" for f in forms):
            raise AutoGrabError("SELECTOR_CHANGED")
        parsed = urlsplit(self.page.url)
        query = parse_qs(parsed.query)
        if parsed.netloc != "bandwagonhost.com" or parsed.path != "/cart.php" or query.get("a") != ["confproduct"] or not query.get("i", [""])[0].isdigit():
            raise AutoGrabError("UNEXPECTED_CART_PAGE")
        return {"product_id": product.product_id, "product_name": product.name,
                "billing": billing, "selected_price": selected, "next_button": "Add to Cart",
                "configuration_url": f"https://bandwagonhost.com/cart.php?a=confproduct&i={query['i'][0]}",
                "form_method": "POST", "configuration_verified": True,
                "product_id_verification": "OBSERVED_ORDER_LINK_AND_EXACT_NAME; PID_NOT_DISPLAYED_ON_CONFIGURATION"}

    async def confirm_empty_cart(self):
        """A fresh, visible empty-cart result is required to replace a verified draft."""
        await self.navigate(self.page, "https://bandwagonhost.com/cart.php?a=view")
        empty = self.page.get_by_text("Your Shopping Cart is Empty", exact=True)
        summary = self.page.get_by_role("heading", name="Order Summary", exact=True)
        return (self.page.url == "https://bandwagonhost.com/cart.php?a=view"
                and await empty.count() == 1 and await empty.is_visible()
                and await summary.count() == 1 and await summary.is_visible())


def write_marker(path: Path, value: dict):
    """Persist dispatch before navigation. Crashes never authorize a replay."""
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
