"""Experimental synthetic order fixtures. All browser requests stay offline.

No current merchant receipt or no-charge checkout selector has been verified.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from uuid import uuid4
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
EXPECTED={"product_id":"87","name":"Experimental VPS","url":"https://bandwagonhost.com/order/ecommerce","period":"annually","cents":16999,"currency":"USD"}


class ExperimentalOrderDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(headless=True)
        self.context=await self.browser.new_context(service_workers="block")
        self.routes={}; self.requests=[]
        async def offline(route):
            self.requests.append((route.request.method,route.request.url))
            if route.request.url in self.routes:
                await route.fulfill(status=200,content_type="text/html",body=self.routes[route.request.url])
            else: await route.abort()
        await self.context.route("**/*",offline)
        self.page=await self.context.new_page()

    async def asyncTearDown(self):
        await self.context.close(); await self.browser.close(); await self.pw.stop()

    async def load(self,html,url,provider="bandwagon"):
        self.routes[url]=html; await self.page.goto(url)
        await self.page.add_script_tag(path=ROOT/f"edge-extension/providers/{provider}/adapter.js")
        await self.page.add_script_tag(path=ROOT/"edge-extension/order-adapter.js")
        await self.page.evaluate("() => {globalThis.orderClicks=0; document.querySelector('form')?.addEventListener('submit',e=>{e.preventDefault();globalThis.orderClicks++;});}")
        self.requests.clear()

    async def call(self,operation,context,expected=EXPECTED):
        return await self.page.evaluate("([op,expected,ctx])=>AutoGrabOrders[op](expected,ctx)",[operation,expected,context])

    async def test_no_charge_requires_positive_evidence_and_single_use_manual_order_boundary(self):
        source=(ROOT/"tests/fixtures/order-checkout.experimental.html").read_text()
        for provider,host in (("bandwagon","https://bandwagonhost.com"),("dmit","https://www.dmit.io")):
            expected={**EXPECTED,"url":host+("/order/ecommerce" if provider=="bandwagon" else "/cart.php?gid=1")}
            for old,new in (("$0.00 USD","$1.00 USD"),("No saved payment methods",""),("Automatic payments disabled",""),("No payment will be taken or credit applied.",""),("banktransfer","paypal"),
                    ('<button type="submit">','<input type="hidden" name="paymentmethod" value="paypal"><button type="submit">'),
                    ('type="submit">Complete Order</button></form>','type="submit" form="payment">Complete Order</button></form><form id="payment" method="post" action="/pay.php"></form>')):
                await self.load(source.replace(old,new),host+"/cart.php?a=checkout",provider)
                result=await self.call("precheck",{"provider":provider},expected)
                self.assertFalse(result["ok"]); self.assertEqual(result["code"],"NO_CHARGE_UNVERIFIED")
                self.assertEqual(await self.page.evaluate("orderClicks"),0)
                self.assertEqual(self.requests,[])
            await self.load(source,host+"/cart.php?a=checkout",provider)
            self.assertTrue((await self.call("precheck",{"provider":provider},expected))["ok"])
            self.assertEqual((await self.call("submitOrder",{"provider":provider},expected))["code"],"ORDER_PERMIT_REJECTED")
            now=datetime.now(timezone.utc)
            permit={"nonce":str(uuid4()),"precheck_id":str(uuid4()),"issued_at":now.isoformat(),"expires_at":(now+timedelta(seconds=30)).isoformat(),"real_order_smoke_test_armed":True}
            interrupted=await self.page.evaluate("([expected,ctx])=>{const result=AutoGrabOrders.submitOrder(expected,ctx);queueMicrotask(()=>queueMicrotask(()=>{document.querySelector('.product-name').textContent='DIFFERENT PRODUCT';}));return result;}",[expected,{"provider":provider,"permit":permit}])
            self.assertEqual(interrupted["action"],"NONE")
            self.assertEqual(await self.page.evaluate("orderClicks"),0)
            await self.page.locator('.product-name').evaluate("n=>n.textContent='Experimental VPS'")
            permit={**permit,"nonce":str(uuid4())}
            result=await self.call("submitOrder",{"provider":provider,"permit":permit},expected)
            self.assertEqual((result["action"],result["outcome"]),("SUBMITTED","UNKNOWN"))
            self.assertEqual((await self.call("submitOrder",{"provider":provider,"permit":permit},expected))["code"],"ORDER_PERMIT_REJECTED")
            self.assertEqual(await self.page.evaluate("orderClicks"),1)
            self.assertEqual(self.requests,[])

    async def test_receipt_needs_exact_identity_time_scope_and_unpaid_invoice_never_pays(self):
        now=datetime.now(timezone.utc)
        ctx={"provider":"bandwagon","submission_nonce":str(uuid4()),"submitted_at":(now-timedelta(seconds=2)).isoformat(),"order_id":None,"invoice_id":None}
        source=(ROOT/"tests/fixtures/order-receipt.experimental.html").read_text().replace("CREATED_AT",now.isoformat())
        await self.load(source,"https://bandwagonhost.com/cart.php?a=complete")
        order=await self.call("reconcileOrder",ctx)
        self.assertEqual((order["outcome"],order["order_id"],order["invoice_id"]),("ORDER_FOUND","501","601"))
        self.assertEqual(order["official_url"],"https://bandwagonhost.com/viewinvoice.php?id=601")
        for bad in ({**ctx,"order_id":"999"},{**ctx,"submitted_at":(now+timedelta(seconds=1)).isoformat()},{**ctx,"submission_nonce":None}):
            self.assertEqual((await self.call("reconcileOrder",bad))["outcome"],"UNKNOWN")
        invoice=source.replace('id="orderConfirmation"','id="invoice"').replace('<a href="/viewinvoice.php?id=601">View Invoice</a>','<p data-role="invoice-status">Unpaid</p><button onclick="globalThis.paid=true">Pay Now</button>')
        await self.load(invoice,"https://bandwagonhost.com/viewinvoice.php?id=601")
        ready=await self.call("reconcileOrder",{**ctx,"order_id":"501","invoice_id":"601"})
        self.assertEqual((ready["outcome"],ready["unpaid"]),("PAYMENT_READY",True))
        self.assertIsNone(await self.page.evaluate("globalThis.paid"))
        await self.page.locator('[data-role="invoice-status"]').evaluate("n=>n.textContent='Paid'")
        self.assertEqual((await self.call("reconcileOrder",ctx))["outcome"],"UNKNOWN")
        self.assertEqual(self.requests,[])

    async def test_unknown_or_login_never_proves_no_order_and_vps_boundary_never_submits(self):
        await self.load('<a href="/logout.php">Logout</a><h1>No orders</h1>',"https://bandwagonhost.com/clientarea.php?action=orders")
        ctx={"provider":"bandwagon","submission_nonce":str(uuid4()),"submitted_at":datetime.now(timezone.utc).isoformat(),"order_id":None,"invoice_id":None}
        self.assertEqual((await self.call("reconcileOrder",ctx))["outcome"],"UNKNOWN")
        await self.page.evaluate("()=>{const n=document.createElement('input');n.type='password';n.value='PRIVATE_CANARY';document.body.append(n);}")
        result=await self.call("reconcileOrder",ctx)
        self.assertEqual(result["outcome"],"LOGIN_REQUIRED"); self.assertNotIn("PRIVATE_CANARY",str(result))
        url="https://vps.hosting/cart/amsterdam-cloud-kvm-vps/"
        self.routes[url]='<form id="cartdetails" method="post" action=""><input type="hidden" name="make" value="order"></form><form id="cartforms" action="?cmd=cart"></form><button type="submit" onclick="submitOrder();return false;">Order Now</button>'
        await self.page.goto(url); await self.page.add_script_tag(path=ROOT/"edge-extension/providers/vps/adapter.js")
        product={"product_id":"148","name":"NRT Starter","url":"https://v.ps/products/cloud-kvm-vps/","period":"monthly","cents":695,"currency":"EUR"}
        result=await self.page.evaluate("p=>AutoGrabVPS.detectPage(p)",product)
        self.assertEqual(result["stage"],"REAL_ORDER_BOUNDARY")
        self.assertEqual((await self.page.evaluate("p=>AutoGrabVPS.addToCart(p)",product))["action"],"NONE")
