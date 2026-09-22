// Apple public-page handoff. Bag and checkout mutations require live evidence.
(() => {
  if (globalThis.AutoGrabApple) return;
  const origins = new Set(["https://www.apple.com", "https://www.apple.com.cn"]);
  const regions = {cn: ["https://www.apple.com.cn", ""], us: ["https://www.apple.com", ""], hk: ["https://www.apple.com", "/hk-zh"],
    tw: ["https://www.apple.com", "/tw"], jp: ["https://www.apple.com", "/jp"], sg: ["https://www.apple.com", "/sg"],
    au: ["https://www.apple.com", "/au"], my: ["https://www.apple.com", "/my"]};
  const visible = node => !!node && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
  const text = () => (document.body?.innerText || "").slice(0, 100000);
  const allowed = () => window.top === window && origins.has(location.origin);
  function challenge() {
    if (!allowed()) return "UNKNOWN";
    return /verify you are human|checking your browser|just a moment|security verification|验证您是真人|确认您是真人|安全验证/i.test(document.title + " " + text()) ? "REQUIRED" : "NONE";
  }
  function login() {
    return [...document.querySelectorAll('input[type="password"]')].some(visible) ? "REQUIRED" : "UNKNOWN";
  }
  function bagBlocked() {
    if (!allowed()) return false;
    if (/\b(?:HTTP|Error|status(?: code)?)\s*[:=]?\s*541\b/i.test(text())) return true;
    // Read only metadata already exposed by this document. Do not request the
    // protected endpoint, inspect bodies, or export URL query/session data.
    const region = Object.values(regions).find(([origin, prefix]) => location.origin === origin && location.pathname.startsWith(prefix + "/shop/"));
    if (!region) return false;
    try {
      return performance.getEntriesByType("resource").some(entry => {
        if (entry.responseStatus !== 541) return false;
        try { const url = new URL(entry.name); return url.origin === region[0] && url.pathname === region[1] + "/shop/fulfillment-messages"; }
        catch { return false; }
      });
    } catch { return false; }
  }
  function result(ok, stage, code, extra = {}) {
    const c = challenge(), l = login(), blocked = bagBlocked();
    return {ok: ok && c === "NONE" && l !== "REQUIRED" && !blocked, stage: c === "REQUIRED" ? "HUMAN_CHALLENGE" : l === "REQUIRED" ? "LOGIN" : blocked ? "UNKNOWN" : stage,
      code: c === "REQUIRED" ? "HUMAN_CHALLENGE_REQUIRED" : l === "REQUIRED" ? "LOGIN_REQUIRED" : blocked ? "APPLE_BAG_BLOCKED" : code, login: l, challenge: c, ...extra};
  }
  function target(expected) {
    const id = /^([a-z]{2}):([A-Z0-9]{1,30}\/A)$/.exec(expected?.product_id || ""), region = id && regions[id[1]];
    if (!region) return null;
    try {
      const url = new URL(expected.url), prefix = region[1] + "/shop/";
      if (url.origin !== region[0] || url.username || url.password || url.search || url.hash ||
          !url.pathname.startsWith(prefix) || !/^(?:buy-[a-z0-9-]+\/|product\/)/.test(url.pathname.slice(prefix.length)) ||
          !/^[A-Za-z0-9/_-]+$/.test(url.pathname) || expected.url !== url.origin + url.pathname) return null;
      const suffix = /\/([A-Za-z0-9]{1,30}\/[Aa])\/?$/.exec(url.pathname);
      if (suffix && suffix[1].toUpperCase() !== id[2]) return null;
      return {region, sku: id[2]};
    } catch { return null; }
  }
  async function detectPage(expected) {
    if (!allowed()) return result(false, "UNKNOWN", "UNEXPECTED_ORIGIN");
    const wanted = target(expected);
    if (!wanted || location.origin !== wanted.region[0] || !location.pathname.startsWith(wanted.region[1] + "/shop/") || location.search || location.hash) return result(false, "UNKNOWN", "APPLE_TARGET_UNVERIFIED");
    const path = location.pathname.slice(wanted.region[1].length);
    if (/^\/shop\/bag\/?$/.test(path)) {
      const bag = [...document.querySelectorAll("h1")].some(node => visible(node) && /bag|购物袋|購物袋/i.test(node.innerText));
      return result(bag, bag ? "CART" : "UNKNOWN", bag ? "APPLE_BAG_PAGE_OBSERVED" : "APPLE_BAG_UNVERIFIED");
    }
    const product = /^\/shop\/(?:buy-[a-z0-9-]+|product)\//.test(path) && [...document.querySelectorAll("h1")].some(visible);
    const suffix = /\/([A-Za-z0-9]{1,30}\/[Aa])\/?$/.exec(path);
    if (product && suffix && suffix[1].toUpperCase() !== wanted.sku) return result(false, "UNKNOWN", "APPLE_SKU_MISMATCH");
    const disabled = [...document.querySelectorAll('button,input[type="submit"],input[type="button"]')].some(button =>
      visible(button) && /^(?:Add to Bag|添加到购物袋|添加至购物袋|加入购物袋|加入購物袋)$/i.test((button.tagName === "INPUT" ? button.value : button.innerText).trim()) &&
      (button.disabled || button.getAttribute("aria-disabled") === "true"));
    if (product && disabled) return result(false, "PRODUCT", "APPLE_BAG_UNAVAILABLE");
    return result(product, product ? "PRODUCT" : "UNKNOWN", product ? suffix ? "APPLE_SKU_PAGE_OBSERVED" : "PUBLIC_PAGE_OBSERVED" : "PAGE_UNVERIFIED");
  }
  async function verifyProduct(expected) {
    const page = await detectPage(expected);
    if (!page.ok) return page;
    // An exact URL or catalog entry does not prove the current UI selection.
    return result(false, "PRODUCT", "APPLE_PRODUCT_IDENTITY_UNVERIFIED");
  }
  async function verifyCart(expected) {
    const page = await detectPage(expected);
    if (!page.ok) return page;
    // No real bag item schema has been verified; do not accept generic text or links.
    return result(false, page.stage === "CART" ? "CART" : "UNKNOWN", "APPLE_BAG_UNVERIFIED");
  }
  async function verifyCheckout() { return result(false, "UNKNOWN", "APPLE_CHECKOUT_UNVERIFIED"); }
  async function readonly() { return result(false, "UNKNOWN", "ADAPTER_READ_ONLY", {action: "NONE"}); }
  Object.defineProperty(globalThis, "AutoGrabApple", {value: Object.freeze({readonly: true,
    detectChallenge: async () => ({challenge: challenge()}), detectPage, verifyProduct, verifyCart, verifyCheckout,
    selectBillingPeriod: readonly, configureProduct: readonly, addToCart: readonly, openCheckout: readonly})});
})();
