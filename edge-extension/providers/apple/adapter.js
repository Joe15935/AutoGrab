// Apple public-page handoff. Bag and checkout mutations require live evidence.
(() => {
  if (globalThis.AutoGrabApple) return;
  const origins = new Set(["https://www.apple.com", "https://www.apple.com.cn"]);
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
  function result(ok, stage, code, extra = {}) {
    const c = challenge(), l = login();
    return {ok: ok && c === "NONE" && l !== "REQUIRED", stage: c === "REQUIRED" ? "HUMAN_CHALLENGE" : l === "REQUIRED" ? "LOGIN" : stage,
      code: c === "REQUIRED" ? "HUMAN_CHALLENGE_REQUIRED" : l === "REQUIRED" ? "LOGIN_REQUIRED" : code, login: l, challenge: c, ...extra};
  }
  async function detectPage() {
    if (!allowed()) return result(false, "UNKNOWN", "UNEXPECTED_ORIGIN");
    const product = /\/shop\/(?:buy-[a-z0-9-]+|product)\//.test(location.pathname) && [...document.querySelectorAll("h1")].some(visible);
    return result(product, product ? "PRODUCT" : "UNKNOWN", product ? "PUBLIC_PAGE_OBSERVED" : "PAGE_UNVERIFIED");
  }
  async function verifyProduct() { return result(false, "PRODUCT", "APPLE_PRODUCT_IDENTITY_UNVERIFIED"); }
  async function verifyCart() { return result(false, "UNKNOWN", "APPLE_BAG_UNVERIFIED"); }
  async function verifyCheckout() { return result(false, "UNKNOWN", "APPLE_CHECKOUT_UNVERIFIED"); }
  async function readonly() { return result(false, "UNKNOWN", "ADAPTER_READ_ONLY", {action: "NONE"}); }
  Object.defineProperty(globalThis, "AutoGrabApple", {value: Object.freeze({readonly: true,
    detectChallenge: async () => ({challenge: challenge()}), detectPage, verifyProduct, verifyCart, verifyCheckout,
    selectBillingPeriod: readonly, configureProduct: readonly, addToCart: readonly, openCheckout: readonly})});
})();
