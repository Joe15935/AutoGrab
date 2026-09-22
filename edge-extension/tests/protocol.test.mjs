import test from "node:test";
import assert from "node:assert/strict";
import {randomUUID} from "node:crypto";
import {validateEnvelope, envelope, isProductURL, sanitizeURL} from "../protocol.js";
const product = {name: "20G KVM - PROMO", url: "https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9", period: "annually", cents: 4999, currency: "USD"};
const command = (type = "START_DRY_RUN") => ({version: 1, type, message_id: randomUUID(), command_id: randomUUID(), intent_id: randomUUID(), product_id: "87", provider: "bandwagon", timestamp: new Date().toISOString(), payload: {product, mode: "DRY_RUN"}});
test("accepts fixed DRY_RUN schema", () => { assert.equal(validateEnvelope(command()).type, "START_DRY_RUN"); });
for (const [name, edit] of [
  ["unknown command", m => m.type = "EXECUTE_SCRIPT"], ["extra command key", m => m.script = "anything"],
  ["extra payload key", m => m.payload = {...m.payload, selector: "button"}], ["LIVE mode", m => m.payload = {...m.payload, mode: "LIVE"}],
  ["non-bandwagon provider", m => m.provider = "another"], ["missing intent", m => m.intent_id = null],
  ["numeric product id", m => m.product_id = 87], ["stale timestamp", m => m.timestamp = "2020-01-01T00:00:00Z"],
  ["future timestamp", m => m.timestamp = new Date(Date.now()+121000).toISOString()],
  ["extra product key", m => m.payload = {...m.payload, product: {...product, cookie: "forbidden"}}],
  ["query-bearing product URL", m => m.payload = {...m.payload, product: {...product, url: product.url+"?x=1"}}],
  ["other host", m => m.payload = {...m.payload, product: {...product, url: "https://example.org/order/ecommerce/x"}}],
]) test(`rejects ${name}`, () => { const value = command(); edit(value); assert.throws(() => validateEnvelope(value)); });
test("control identity must be null and payload empty", () => {
  const value = {...command(), type: "PING", intent_id: null, product_id: null, payload: {}};
  assert.equal(validateEnvelope(value).type, "PING"); assert.throws(() => validateEnvelope({...value, intent_id: randomUUID()}));
});
test("telemetry strips challenge/account identifiers", () => {
  assert.equal(sanitizeURL("https://bandwagonhost.com/clientarea.php?challenge=value"), "https://bandwagonhost.com/clientarea.php");
  assert.equal(sanitizeURL("https://bandwagonhost.com/viewinvoice.php?id=999"), "https://bandwagonhost.com/");
  assert.equal(sanitizeURL("https://bandwagonhost.com/cart.php?a=confproduct&i=0&token=value"), "https://bandwagonhost.com/cart.php?a=confproduct");
  assert.equal(sanitizeURL("https://evil.example/cart.php"), null); assert.equal(isProductURL("https://user@bandwagonhost.com/order/ecommerce/x"), false);
});
test("events reject account fields and unsanitized URLs", () => {
  const id = {intent_id: randomUUID(), product_id: "87"};
  assert.throws(() => envelope("PAGE_OPENED", id, {password: "forbidden"}, randomUUID(), randomUUID));
  assert.throws(() => envelope("PAGE_OPENED", id, {url: "https://bandwagonhost.com/clientarea.php?x=y"}, randomUUID(), randomUUID));
});
test("product URL contract accepts root and rejects normalized traversal",()=>{
  assert.equal(isProductURL("https://bandwagonhost.com/order/ecommerce"),true);
  assert.equal(isProductURL("https://bandwagonhost.com/x/../order/ecommerce/A/B"),false);
  assert.equal(isProductURL("https://bandwagonhost.com/order/ecommerce/A/%2E%2E/A/B"),false);
  assert.equal(isProductURL("https://bandwagonhost.com/order/ecommerce/洛杉矶/B"),false);
});
test("provider URLs bind product identity and Apple region without weakening Bandwagon", () => {
  const cases = [
    ["dmit", "87", "https://www.dmit.io/cart.php?gid=1", true],
    ["dmit", "87", "https://www.dmit.io/cart.php?a=add&pid=88", false],
    ["vmiss", "vps/basic", "https://app.vmiss.com/store/vps/basic", true],
    ["vmiss", "vps/basic", "https://app.vmiss.com/store/other/basic", false],
    ["vps", "148", "https://vps.hosting/?cmd=cart&action=add&id=148", true],
    ["vps", "148", "https://vps.hosting/?cmd=cart&action=add&id=149", false],
    ["apple", "cn:MYEV3CH/A", "https://www.apple.com.cn/shop/buy-iphone/iphone-16/myev3ch/a", true],
    ["apple", "us:MYEV3CH/A", "https://www.apple.com.cn/shop/buy-iphone/iphone-16/myev3ch/a", false],
    ["apple", "cn:MYEV3CH/A", "https://www.apple.com.cn/shop/buy-iphone/iphone-16/other/a", false],
    ["apple", "hk:MYEV3ZP/A", "https://www.apple.com/hk-zh/shop/product/MYEV3ZP/A", true],
  ];
  for (const [provider, id, url, valid] of cases) assert.equal(isProductURL(url, provider, id), valid, url);
});
test("unknown prices permit only read-only URLs and cannot start or resume", () => {
  const value = {...command("OPEN_PRODUCT"), provider:"dmit", payload:{mode:"DRY_RUN", product:{...product, url:"https://www.dmit.io/cart.php?gid=1", period:"unknown", cents:null, currency:null}}};
  assert.equal(validateEnvelope(value).type,"OPEN_PRODUCT");
  for (const type of ["START_DRY_RUN","RESUME_INTENT"]) assert.throws(()=>validateEnvelope({...value,type}), /VERIFIED_PRICE_REQUIRED/);
  assert.throws(()=>validateEnvelope({...value,payload:{...value.payload,product:{...value.payload.product,url:"https://www.dmit.io/cart.php?a=add&pid=87"}}}), /READ_ONLY_PRODUCT_URL_REQUIRED/);
  assert.throws(()=>validateEnvelope({...command("PING"),provider:"apple",intent_id:null,product_id:null,payload:{}}));
});
