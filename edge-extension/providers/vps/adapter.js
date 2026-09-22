/* V.PS public DOM reader. The 2026-09-22 official family cards expose numeric
 * HostBill links; their Order label is not proof of current inventory.
 * No cart/checkout action is enabled without separately observed evidence. */
(() => {
  "use strict";
  if (Object.hasOwn(globalThis, "AutoGrabVPS")) return;
  const norm = value => String(value || "").replace(/\s+/g, " ").trim();
  const text = node => norm(node?.innerText || node?.textContent);
  const visible = node => !!node && !node.hidden && node.getAttribute("aria-hidden") !== "true" &&
    !!node.getClientRects().length && getComputedStyle(node).display !== "none" && getComputedStyle(node).visibility !== "hidden";
  const all = selector => [...document.querySelectorAll(selector)].filter(visible);
  function url(raw) {
    try {
      const value = new URL(raw, location.href);
      return value.protocol === "https:" && !value.username && !value.password && !value.hash &&
        !/[\r\n\t]/.test(raw) && ["https://v.ps", "https://vps.hosting"].includes(value.origin) ? value : null;
    } catch { return null; }
  }
  const family = value => value && value.origin === "https://v.ps" && !value.search && /^\/products\/[a-z0-9-]+-kvm-vps\/$/.test(value.pathname);
  function order(value, id) {
    const valueURL = url(value);
    return valueURL && valueURL.origin === "https://vps.hosting" && valueURL.pathname === "/" &&
      [...valueURL.searchParams.keys()].sort().join(",") === "action,cmd,id" &&
      valueURL.searchParams.get("cmd") === "cart" && valueURL.searchParams.get("action") === "add" &&
      valueURL.searchParams.get("id") === id;
  }
  function expected(value) {
    if (!value || Object.getPrototypeOf(value) !== Object.prototype ||
        Object.keys(value).sort().join(",") !== "cents,currency,name,period,product_id,url" ||
        typeof value.product_id !== "string" || !/^[1-9][0-9]{0,39}$/.test(value.product_id) ||
        typeof value.name !== "string" || !value.name || value.name !== norm(value.name) || value.name.length > 200 ||
        !family(url(value.url)) || ![null, "monthly", "annually"].includes(value.period) ||
        ![null, "EUR"].includes(value.currency) ||
        !(value.cents === null || Number.isSafeInteger(value.cents) && value.cents > 0 && value.cents < 1e12)) throw new Error("INVALID_EXPECTED_PRODUCT");
  }
  function challenge() {
    const surface = norm(document.title + " " + document.body?.innerText);
    return /just a moment|verify (?:that )?you are (?:a )?human|checking your browser|验证您是真人/i.test(surface) ||
      all('iframe[src*="challenges.cloudflare.com"],.cf-turnstile,#challenge-running,#challenge-stage').length ? "REQUIRED" : "NONE";
  }
  function login() {
    return challenge() === "REQUIRED" ? "UNKNOWN" : all('input[type="password"]').length ? "REQUIRED" : "UNKNOWN";
  }
  function stage() {
    if (challenge() === "REQUIRED") return "HUMAN_CHALLENGE";
    if (!url(location.href) || window.top !== window) return "UNKNOWN";
    if (login() === "REQUIRED") return "LOGIN";
    return family(url(location.href)) ? "PRODUCT" : "UNKNOWN";
  }
  const result = (ok, code, extra = {}) => ({ok, code, stage: stage(), login: login(), challenge: challenge(), ...extra});
  function guard() {
    if (!url(location.href) || window.top !== window) return result(false, "SITE_CHANGED");
    if (challenge() === "REQUIRED") return result(false, "HUMAN_CHALLENGE_REQUIRED");
    if (login() === "REQUIRED") return result(false, "LOGIN_REQUIRED");
    return null;
  }
  async function verifyProduct(value) {
    expected(value);
    const stopped = guard(); if (stopped) return stopped;
    if (location.href !== value.url || stage() !== "PRODUCT") return result(false, "PRODUCT_UNVERIFIED");
    const cards = all("a[href]").filter(node => order(node.href, value.product_id));
    if (cards.length !== 1 || cards[0].getAttribute("title") !== "Order " + value.name ||
        !text(cards[0]).startsWith(value.name)) return result(false, "PRODUCT_MISMATCH");
    const prices = [...text(cards[0]).matchAll(/€\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})\s*\/\s*(mo|yr)\b/g)];
    if (prices.length !== 1) return result(false, "PRICE_UNVERIFIED");
    const [whole, fraction] = prices[0][1].replaceAll(",", "").split(".");
    const cents = Number(whole) * 100 + Number(fraction), period = prices[0][2] === "mo" ? "monthly" : "annually";
    if (value.cents !== null && value.cents !== cents || value.period !== null && value.period !== period) return result(false, "PRICE_MISMATCH");
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
  Object.defineProperty(globalThis, "AutoGrabVPS", {value: Object.freeze(api), writable: false, configurable: false});
})();
