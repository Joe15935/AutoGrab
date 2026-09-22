// DMIT uses its own theme. Do not apply Bandwagon/WHMCS form assumptions.
(() => {
  if (globalThis.AutoGrabDMIT) return;
  const visible = node => !!node && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
  const allowed = () => window.top === window && location.origin === "https://www.dmit.io";
  function challenge() {
    if (!allowed()) return "UNKNOWN";
    return /verify you are human|checking your browser|just a moment|验证您是真人|请稍候|cloudflare ray id/i.test(document.title + " " + (document.body?.innerText || "").slice(0, 100000)) ? "REQUIRED" : "NONE";
  }
  function result(ok, stage, code, extra = {}) {
    const c = challenge(), login = [...document.querySelectorAll('input[type="password"]')].some(visible) ? "REQUIRED" : "UNKNOWN";
    return {ok: ok && c === "NONE" && login !== "REQUIRED", stage: c === "REQUIRED" ? "HUMAN_CHALLENGE" : login === "REQUIRED" ? "LOGIN" : stage,
      code: c === "REQUIRED" ? "HUMAN_CHALLENGE_REQUIRED" : login === "REQUIRED" ? "LOGIN_REQUIRED" : code, login, challenge: c, ...extra};
  }
  async function detectPage() {
    const product = allowed() && (location.pathname === "/cart.php" || location.pathname.startsWith("/store/")) && [...document.querySelectorAll("h1,h2")].some(visible);
    return result(product, product ? "PRODUCT" : "UNKNOWN", product ? "PUBLIC_PAGE_OBSERVED" : "PAGE_UNVERIFIED");
  }
  async function verifyProduct() { return result(false, "PRODUCT", "DMIT_PRODUCT_IDENTITY_UNVERIFIED"); }
  async function verifyCart() { return result(false, "UNKNOWN", "DMIT_CART_UNVERIFIED"); }
  async function verifyCheckout() { return result(false, "UNKNOWN", "DMIT_CHECKOUT_UNVERIFIED"); }
  async function readonly() { return result(false, "UNKNOWN", "ADAPTER_READ_ONLY", {action: "NONE"}); }
  Object.defineProperty(globalThis, "AutoGrabDMIT", {value: Object.freeze({readonly: true,
    detectChallenge: async () => ({challenge: challenge()}), detectPage, verifyProduct, verifyCart, verifyCheckout,
    selectBillingPeriod: readonly, configureProduct: readonly, addToCart: readonly, openCheckout: readonly})});
})();
