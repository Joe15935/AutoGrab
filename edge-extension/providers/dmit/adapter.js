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
    const cartPage = allowed() && location.pathname === "/cart.php";
    const action = cartPage ? new URL(location.href).searchParams.get("a") : null;
    const text = document.body?.innerText || "";
    // These markers identify a page only. They do not independently bind a PID,
    // prove a cart, or authorize any checkout/order action.
    if (action === "view") {
      const checkout = [...document.querySelectorAll("a[href]")].some(link => {
        if (!visible(link) || link.innerText.trim() !== "Checkout") return false;
        try { return new URL(link.getAttribute("href"), location.href).href === "https://www.dmit.io/cart.php?a=checkout"; }
        catch { return false; }
      });
      const cart = text.includes("Review & Checkout") && checkout;
      return result(cart, cart ? "CART" : "UNKNOWN", cart ? "CART_PAGE_OBSERVED" : "PAGE_UNVERIFIED");
    }
    if (action === "checkout") {
      const complete = [...document.querySelectorAll('button,input[type="submit"]')].some(button =>
        visible(button) && (button.tagName === "INPUT" ? button.value : button.innerText).trim() === "Complete Order");
      const checkout = text.includes("Personal Information") && text.includes("Payment Details") && complete;
      return result(checkout, checkout ? "CHECKOUT" : "UNKNOWN", checkout ? "CHECKOUT_PAGE_OBSERVED" : "PAGE_UNVERIFIED");
    }
    const configuration = action === "confproduct" && document.querySelector("form#frmConfigureProduct");
    if (configuration && visible(configuration)) {
      const button = configuration.querySelector("#btnCompleteProductConfig");
      // The merchant leaves this icon spinning after some failed requests. It
      // proves neither a dispatch nor its outcome, so never infer a retry here.
      const pending = visible(button) && [...button.querySelectorAll(".fa-spinner.fa-spin")].some(visible);
      const errors = document.querySelector("#containerProductValidationErrors");
      const list = errors?.querySelector("#containerProductValidationErrorsList");
      // Report only the presence of visible validation errors, never their text
      // or form values. A prior error may remain during an uncertain submission.
      const invalid = visible(errors) && visible(list) && !!list.innerText.trim();
      return result(!pending && !invalid, "CONFIGURATION", pending ? "CONFIGURATION_UNCERTAIN" : invalid ? "CONFIGURATION_INVALID" : "CONFIGURATION_PAGE_OBSERVED");
    }
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
