/* Experimental VMISS Lagom public-card reader. Current HTTP store access was
 * challenge-blocked; the class contract is reference-derived and needs normal
 * Edge validation. Exact identity/price must match; no cart action is enabled. */
(() => {
  "use strict";
  if (Object.hasOwn(globalThis, "AutoGrabVMISS")) return;
  const norm = value => String(value || "").replace(/\s+/g, " ").trim();
  const text = node => norm(node?.innerText || node?.textContent);
  const visible = node => !!node && !node.hidden && node.getAttribute("aria-hidden") !== "true" &&
    !!node.getClientRects().length && getComputedStyle(node).display !== "none" && getComputedStyle(node).visibility !== "hidden";
  const all = (selector, root = document) => [...root.querySelectorAll(selector)].filter(visible);
  const slug = /^[a-z0-9][a-z0-9-]{0,79}\/[a-z0-9][a-z0-9-]{0,79}$/;
  function official(raw) {
    try {
      const value = new URL(raw, location.href);
      return value.origin === "https://app.vmiss.com" && !value.username && !value.password && !value.hash && !value.search &&
        !/[\r\n\t]/.test(raw) ? value : null;
    } catch { return null; }
  }
  function store(value) { return value && /^\/store(?:\/[a-z0-9][a-z0-9-]{0,79}){1,2}\/?$/.test(value.pathname); }
  function expected(value) {
    if (!value || Object.getPrototypeOf(value) !== Object.prototype ||
        Object.keys(value).sort().join(",") !== "cents,currency,name,period,product_id,url" ||
        typeof value.product_id !== "string" || !slug.test(value.product_id) ||
        typeof value.name !== "string" || !value.name || value.name !== norm(value.name) || value.name.length > 200 ||
        !store(official(value.url)) || ![null, "monthly", "quarterly", "semiannually", "annually", "biennially", "triennially"].includes(value.period) ||
        ![null, "CAD", "USD", "EUR", "GBP"].includes(value.currency) ||
        !(value.cents === null || Number.isSafeInteger(value.cents) && value.cents > 0 && value.cents < 1e12)) throw new Error("INVALID_EXPECTED_PRODUCT");
  }
  function challenge() {
    return /just a moment|verify (?:that )?you are (?:a )?human|checking your browser|验证您是真人/i.test(norm(document.title + " " + document.body?.innerText)) ||
      all('iframe[src*="challenges.cloudflare.com"],.cf-turnstile,#challenge-running,#challenge-stage').length ? "REQUIRED" : "NONE";
  }
  const login = () => challenge() === "REQUIRED" ? "UNKNOWN" : all('input[type="password"]').length ? "REQUIRED" : "UNKNOWN";
  function stage() {
    if (challenge() === "REQUIRED") return "HUMAN_CHALLENGE";
    if (!official(location.href) || window.top !== window) return "UNKNOWN";
    if (login() === "REQUIRED") return "LOGIN";
    return store(official(location.href)) ? "PRODUCT" : "UNKNOWN";
  }
  const result = (ok, code, extra = {}) => ({ok, code, stage: stage(), login: login(), challenge: challenge(), ...extra});
  function guard() {
    if (!official(location.href) || window.top !== window) return result(false, "SITE_CHANGED");
    if (challenge() === "REQUIRED") return result(false, "HUMAN_CHALLENGE_REQUIRED");
    if (login() === "REQUIRED") return result(false, "LOGIN_REQUIRED");
    return null;
  }
  async function verifyProduct(value) {
    expected(value); const stopped = guard(); if (stopped) return stopped;
    if (location.href !== value.url || stage() !== "PRODUCT") return result(false, "PRODUCT_UNVERIFIED");
    const target = "/store/" + value.product_id;
    const cards = all(".package").filter(card => all(".btn-order-now", card).some(node => official(node.href)?.pathname === target));
    if (cards.length !== 1) return result(false, "PRODUCT_UNVERIFIED");
    const names = all(".package-title", cards[0]), amounts = all(".price-amount", cards[0]), cycles = all(".price-cycle", cards[0]);
    if (names.length !== 1 || text(names[0]) !== value.name || amounts.length !== 1 || cycles.length !== 1) return result(false, "PRODUCT_MISMATCH");
    const matches = [...text(amounts[0]).matchAll(/([0-9]+(?:,[0-9]{3})*\.[0-9]{2})\s*(CAD|USD|EUR|GBP)\b/g)];
    if (matches.length !== 1) return result(false, "PRICE_UNVERIFIED");
    const [whole, fraction] = matches[0][1].replaceAll(",", "").split(".");
    const cents = Number(whole) * 100 + Number(fraction), period = text(cycles[0]).replace(/^[ /-]+|[ /-]+$/g, "").toLowerCase();
    if (value.cents !== null && value.cents !== cents || value.currency !== null && value.currency !== matches[0][2] ||
        value.period !== null && value.period !== period) return result(false, "PRICE_MISMATCH");
    return result(true, "PUBLIC_PRODUCT_VERIFIED");
  }
  const refuse = async () => ({...(guard() || result(false, "ADAPTER_READ_ONLY")), action: "NONE"});
  const api = {
    readonly: true,
    async detectPage(value) { expected(value); return guard() || result(stage() === "PRODUCT", stage() === "PRODUCT" ? "PAGE_OPENED" : "PAGE_UNVERIFIED"); },
    async detectChallenge() { return guard() || result(true, "NO_CHALLENGE"); },
    async detectLogin() { return guard() || result(false, "LOGIN_UNVERIFIED"); },
    verifyProduct,
    async verifyCart() { return guard() || result(false, "CART_UNVERIFIED"); },
    async verifyCheckout() { return guard() || result(false, "CHECKOUT_UNVERIFIED"); },
    selectBillingPeriod: refuse, configureProduct: refuse, addToCart: refuse, openCheckout: refuse,
    async detectOrder() { return result(false, "ORDER_UNVERIFIED"); },
    async detectInvoice() { return result(false, "INVOICE_UNVERIFIED"); },
    async detectPaymentReady() { return result(false, "PAYMENT_READY_UNVERIFIED"); }
  };
  Object.defineProperty(globalThis, "AutoGrabVMISS", {value: Object.freeze(api), writable: false, configurable: false});
})();
