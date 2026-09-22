/* AutoGrab BandwagonHost DOM adapter. No network, cookie or account-data API.
 * Page structure remains an unverified live contract until exercised in Edge.
 * PID continuity after a=add belongs to the worker's durable navigation journal:
 * the merchant does not expose PID on its observed configuration page.
 */
(() => {
  "use strict";
  // A wrapper retry may re-inject this file. Preserve this document's dispatch
  // tombstones instead of replacing the frozen adapter or permitting a replay.
  if (Object.hasOwn(globalThis, "AutoGrabBandwagon")) return;
  const ORIGIN = "https://bandwagonhost.com";
  const PERIODS = {annually: /\bannually\b/i, biennially: /\bbiennially\b/i};
  const dispatched = new Set();
  const norm = value => String(value || "").replace(/\s+/g, " ").trim();
  const visible = node => !!node && !node.hidden && !!node.getClientRects().length &&
    getComputedStyle(node).visibility !== "hidden" && getComputedStyle(node).display !== "none";
  const all = selector => [...document.querySelectorAll(selector)].filter(visible);
  const text = node => norm(node?.innerText || node?.textContent || "");
  const heading = label => all("h1,h2,h3").filter(node => text(node) === label).length === 1;
  function official(raw) {
    try {
      const url = new URL(raw, location.href);
      if (url.origin !== ORIGIN || url.username || url.password || url.hash || /[\r\n\t]/.test(raw)) return null;
      return url;
    } catch { return null; }
  }
  function route(raw, action, extra = null) {
    const url = official(raw);
    if (!url || url.pathname !== "/cart.php" || url.searchParams.getAll("a").length !== 1 || url.searchParams.get("a") !== action) return null;
    const keys = [...url.searchParams.keys()];
    if (extra === "i") {
      if (keys.length !== 2 || url.searchParams.getAll("i").length !== 1 || !/^(0|[1-9][0-9]*)$/.test(url.searchParams.get("i"))) return null;
    } else if (keys.length !== 1) return null;
    return url;
  }
  function checkExpected(expected) {
    if (!expected || Object.getPrototypeOf(expected) !== Object.prototype ||
        Object.keys(expected).sort().join(",") !== "cents,currency,name,period,product_id,url" ||
        typeof expected.product_id !== "string" || !/^[1-9][0-9]{0,39}$/.test(expected.product_id) || typeof expected.name !== "string" ||
        !expected.name || expected.name.length > 200 || expected.name !== norm(expected.name) ||
        !Object.hasOwn(PERIODS, expected.period) || !Number.isSafeInteger(expected.cents) || expected.cents <= 0 || expected.cents >= 1e12 ||
        expected.currency !== "USD") throw new Error("INVALID_EXPECTED_PRODUCT");
    const url = official(expected.url);
    if (!url || !/^\/order\/ecommerce(?:\/[^/]+\/[^/]+)?\/?$/.test(url.pathname) || url.search) throw new Error("INVALID_EXPECTED_PRODUCT");
    return expected;
  }
  function challengeState() {
    const surface = norm(document.title + " " + (document.body?.innerText || "")).toLowerCase();
    if (/just a moment|attention required|verify (?:that )?you are human|checking your browser|verify you are a human|人机验证|验证您是真人/.test(surface) ||
        all('iframe[src*="challenges.cloudflare.com"],.cf-turnstile,#challenge-running,#challenge-stage,input[name="captcha"]').length) return "REQUIRED";
    return "NONE";
  }
  function loginState() {
    if (!official(location.href) || challengeState() === "REQUIRED") return "UNKNOWN";
    const loginForm = [...document.forms].some(form => {
      const target = official(form.action);
      return target && ["/dologin.php", "/login.php"].includes(target.pathname) &&
        [...form.querySelectorAll('input[type="password"]')].some(visible);
    });
    if (loginForm || all('a[href="/cart.php?a=login"],a[href="cart.php?a=login"]').length ||
        (heading("Login") && all('input[type="password"]').length)) return "REQUIRED";
    const logout = all("a[href]").filter(node => {
      const url = official(node.href);
      return url && url.pathname === "/logout.php" && !url.search;
    });
    return logout.length === 1 ? "VALID" : "UNKNOWN";
  }
  function pageStage() {
    if (challengeState() === "REQUIRED") return "HUMAN_CHALLENGE";
    const url = official(location.href);
    if (!url || window.top !== window) return "UNKNOWN";
    if (/^\/order\/ecommerce(?:\/[^/]+\/[^/]+)?\/?$/.test(url.pathname) && !url.search) return "PRODUCT";
    if (route(url.href, "confproduct", "i")) return "CONFIGURATION";
    if (route(url.href, "view")) return "CART";
    if (route(url.href, "checkout")) return "CHECKOUT";
    if (url.pathname === "/viewinvoice.php" && [...url.searchParams].length === 1 && /^[1-9][0-9]*$/.test(url.searchParams.get("id"))) return "INVOICE";
    if (route(url.href, "complete")) return "ORDER";
    if (loginState() === "REQUIRED") return "LOGIN";
    return "UNKNOWN";
  }
  function result(ok, code, extra = {}) {
    return {ok, stage: pageStage(), code, login: loginState(), challenge: challengeState(), ...extra};
  }
  function guard(cartMutation = false) {
    if (window.top !== window || !official(location.href)) return result(false, "SITE_CHANGED");
    if (challengeState() === "REQUIRED") return result(false, "HUMAN_CHALLENGE_REQUIRED");
    // This adapter only changes a DRY_RUN public cart, never creates an order.
    // Normal catalog/config/cart templates do not expose a reliable account
    // marker. UNKNOWN stays UNKNOWN; an explicit login prompt still pauses.
    if (cartMutation && loginState() === "REQUIRED") return result(false, "LOGIN_REQUIRED");
    return null;
  }
  function nameMatch(node, expected) {
    return [expected.name, "VPS - Self-managed - " + expected.name].includes(text(node));
  }
  function moneyMatches(value, expected) {
    const prices = [...norm(value).matchAll(/(?:US\$|\$)?\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})\s*USD\b/g)];
    return prices.length > 0 && prices.every(match => {
      const [whole, decimal] = match[1].replaceAll(",", "").split(".");
      return Number(whole) * 100 + Number(decimal) === expected.cents;
    }) && PERIODS[expected.period].test(value);
  }
  function selectedOption(expected) {
    const selects = all('select[name="billingcycle"]');
    if (selects.length !== 1 || selects[0].selectedOptions.length !== 1) return {error: "SITE_CHANGED"};
    const select = selects[0];
    if (select.value !== expected.period) return {error: "BILLING_MISMATCH"};
    if (!moneyMatches(select.selectedOptions[0].textContent, expected)) return {error: "PRICE_MISMATCH"};
    return {select};
  }
  function submitControl(label) {
    return all('button,input[type="submit"],input[type="button"]').filter(node =>
      norm(node instanceof HTMLInputElement ? node.value : node.innerText) === label);
  }
  function configEvidence(expected) {
    const url = route(location.href, "confproduct", "i");
    if (!url || !heading("Product Configuration")) return {error: "SITE_CHANGED"};
    if (all("strong").filter(node => nameMatch(node, expected)).length !== 1) return {error: "PRODUCT_MISMATCH"};
    const selection = selectedOption(expected);
    if (selection.error) return selection;
    const buttons = submitControl("Add to Cart");
    if (buttons.length !== 1 || buttons[0].disabled || buttons[0].type !== "submit") return {error: "SITE_CHANGED"};
    const button = buttons[0], form = button.form;
    if (!form || form.method !== "post" || form.action !== url.href || selection.select.form !== form ||
        form.target && form.target !== "_self" || button.hasAttribute("formaction") || button.hasAttribute("formmethod") || button.hasAttribute("formtarget")) return {error: "UNSAFE_FORM_ACTION"};
    const pids = [...form.querySelectorAll('input[name="pid"]')];
    if (pids.length && (pids.length !== 1 || pids[0].value !== expected.product_id)) return {error: "PRODUCT_MISMATCH"};
    return {button, form, cart_id: "configuration_" + url.searchParams.get("i")};
  }
  function itemEvidence(expected, checkout = false) {
    if (!route(location.href, checkout ? "checkout" : "view") || !heading(checkout ? "Checkout" : "Order Summary")) return {error: "SITE_CHANGED"};
    if (!checkout && emptyCart()) return {error: "CART_EMPTY"};
    const names = all("strong").filter(node => nameMatch(node, expected));
    const products = all("strong").filter(node => /^VPS\s*-\s*Self-managed\s*-/i.test(text(node)));
    if (names.length !== 1 || products.some(node => !nameMatch(node, expected)) || products.length > 1) return {error: "CART_REVIEW_REQUIRED"};
    const row = names[0].closest("tr");
    if (!row || !visible(row)) return {error: "CART_REVIEW_REQUIRED"};
    // Require a single, explicit product row. Any additional priced item is unknown.
    const table = row.closest("table");
    if (!table) return {error: "CART_REVIEW_REQUIRED"};
    const amountOnly = value => {
      const prices = [...norm(value).matchAll(/(?:US\$|\$)?\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})\s*USD\b/g)];
      if (prices.length !== 1) return false;
      const [whole, decimal] = prices[0][1].replaceAll(",", "").split(".");
      return Number(whole) * 100 + Number(decimal) === expected.cents;
    };
    if (!amountOnly(text(row))) return {error: "CART_REVIEW_REQUIRED"};
    // The observed merchant cart places billing under Total Recurring, rather
    // than in the product row. Require one matching summary if one is present.
    const recurring = [...table.querySelectorAll("tr")].filter(node => visible(node) && /^Total Recurring\b/.test(text(node)));
    const billingTerms = node => text(node).toLowerCase().match(/\b(?:monthly|quarterly|semiannually|annually|biennially|triennially)\b/g) || [];
    const rowBilling = billingTerms(row), recurringBilling = recurring.length === 1 ? billingTerms(recurring[0]) : [];
    if (rowBilling.length && (rowBilling.length !== 1 || rowBilling[0] !== expected.period)) return {error: "CART_REVIEW_REQUIRED"};
    if (recurring.length > 1 || (recurring.length === 1 && (!amountOnly(text(recurring[0])) || recurringBilling.length !== 1 || recurringBilling[0] !== expected.period)) ||
        (!rowBilling.length && recurring.length !== 1)) return {error: "CART_REVIEW_REQUIRED"};
    const pricedRows = all("tr").filter(node => /[$€£¥]\s*[0-9]+(?:,[0-9]{3})*\.[0-9]{2}|\b[0-9]+\.[0-9]{2}\s*[A-Z]{3}\b/.test(text(node)));
    if (pricedRows.some(node => !table.contains(node))) return {error: "CART_REVIEW_REQUIRED"};
    const otherRows = pricedRows.filter(node => node !== row);
    if (otherRows.some(node => !/^(Subtotal|Total Due Today|Total Recurring|Total|Tax|VAT)\b/.test(text(node)))) return {error: "CART_REVIEW_REQUIRED"};
    const quantity = [...row.querySelectorAll('input[name*="qty"],input[name*="quantity"],select[name*="qty"],select[name*="quantity"]')];
    if (quantity.some(node => node.value !== "1")) return {error: "CART_REVIEW_REQUIRED"};
    const edits = all("a[href]").filter(node => route(node.href, "confproduct", "i"));
    if ((!checkout && edits.length !== 1) || edits.length > 1) return {error: "CART_REVIEW_REQUIRED"};
    if (edits.length && !row.contains(edits[0])) {
      const editRow = edits[0].closest("tr");
      const controlsOnly = editRow && !text(editRow).replace(/[\[\]]/g, "").replace(/Edit Configuration|Remove/g, "").trim();
      if (!editRow || editRow !== row.nextElementSibling || editRow.closest("table") !== table || !controlsOnly ||
          /[0-9]+\.[0-9]{2}/.test(text(editRow)) || editRow.querySelector("strong,input,select") ||
          [...editRow.querySelectorAll("a,button")].some(node => !["Edit Configuration", "Remove"].includes(text(node).replace(/[\[\]]/g, "").trim()))) return {error: "CART_REVIEW_REQUIRED"};
    }
    return {cart_id: edits.length ? "configuration_" + route(edits[0].href, "confproduct", "i").searchParams.get("i") : undefined};
  }
  function emptyCart() {
    // Explicit live empty state, never absence of a product selector. The
    // observed classic cart has an empty row, two zero totals and disabled
    // Checkout; any other item, price or configuration link prevents rebuild.
    const rows = all("tr").filter(row => text(row) === "Your Shopping Cart is Empty");
    const table = rows.length === 1 && rows[0].closest("table");
    const buttons = submitControl("Checkout");
    if (!table || buttons.length !== 1 || !buttons[0].disabled ||
        all("a[href]").some(node => route(node.href, "confproduct", "i"))) return false;
    const contents = [...table.querySelectorAll("tr")].filter(visible).map(text);
    const subtotal = /^Subtotal:\s*\$0\.00 USD$/;
    const total = /^Total Due Today:\s*\$0\.00 USD$/;
    return contents.filter(value => subtotal.test(value)).length === 1 &&
      contents.filter(value => total.test(value)).length === 1 &&
      contents.every(value => value === "Your Shopping Cart is Empty" || value === "Description Price" || subtotal.test(value) || total.test(value)) &&
      !all("tr").some(row => !table.contains(row) && /\$\s*[0-9]+\.[0-9]{2}/.test(text(row)));
  }
  function observedLinks(expected, selectedPeriod = true) {
    return all("a[href]").filter(node => {
      const url = official(node.href);
      if (!url || url.pathname !== "/cart.php") return false;
      const keys = [...url.searchParams.keys()];
      return url.searchParams.getAll("a").length === 1 && url.searchParams.get("a") === "add" &&
        url.searchParams.getAll("pid").length === 1 && url.searchParams.get("pid") === expected.product_id &&
        url.searchParams.getAll("billingcycle").length === 1 &&
        (selectedPeriod ? url.searchParams.get("billingcycle") === expected.period :
          ["monthly", "quarterly", "semiannually", "annually", "biennially"].includes(url.searchParams.get("billingcycle"))) &&
        keys.every(key => ["a", "pid", "billingcycle", "configoption[17]"].includes(key)) &&
        url.searchParams.getAll("configoption[17]").length <= 1 &&
        (!url.searchParams.has("configoption[17]") || /^[0-9]+$/.test(url.searchParams.get("configoption[17]")));
    });
  }
  function publicMoneyMatches(value, expected) {
    // The observed SPA omits currency and the catalog product name. This is
    // only a public price/link check; exact USD/name verification waits for the
    // configuration page and remains mandatory before its form can be posted.
    const prices = [...norm(value).matchAll(/\$\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})(?![0-9])(?:\s+([A-Z]{3})\b)?/g)];
    const periodVisible = expected.period === "annually" && /\bannually\b|\/\s*(?:1\s*)?year\b/i.test(value);
    return periodVisible && prices.length === 1 && (!prices[0][2] || prices[0][2] === "USD") && (() => {
      const [whole, decimal] = prices[0][1].replaceAll(",", "").split(".");
      return Number(whole) * 100 + Number(decimal) === expected.cents;
    })();
  }
  function configurationLink(expected) {
    if (location.href !== expected.url || pageStage() !== "PRODUCT") return {error: "PRODUCT_MISMATCH"};
    const named = all("h1,h2,h3,h4,strong").filter(node => nameMatch(node, expected));
    if (named.length === 1) {
      const scope = named[0].closest("article,section,tr,.product,.plan,.card") || named[0].parentElement;
      if (scope && scope !== document.body && /\b(?:sold out|out of stock)\b/i.test(text(scope))) return {error: "SOLD_OUT"};
    }
    const links = observedLinks(expected);
    if (links.length !== 1 || links[0].getAttribute("aria-disabled") === "true" || links[0].hasAttribute("disabled")) return {error: "SITE_CHANGED"};
    const link = links[0];
    let container = link.parentElement;
    while (container && container !== document.body) {
      const productIDs = [...container.querySelectorAll('a[href*="cart.php"]')].map(node => official(node.href)?.searchParams.get("pid")).filter(Boolean);
      if (productIDs.length && productIDs.every(pid => pid === expected.product_id) && publicMoneyMatches(text(container), expected)) return {link};
      container = container.parentElement;
    }
    return {error: "PRODUCT_MISMATCH"};
  }
  async function verifyProduct(expected) {
    checkExpected(expected);
    const rejected = guard(); if (rejected) return rejected;
    if (pageStage() === "PRODUCT") {
      const evidence = configurationLink(expected);
      return result(!evidence.error, evidence.error || "PUBLIC_PRODUCT_VERIFIED");
    }
    const evidence = configEvidence(expected);
    return result(!evidence.error, evidence.error || "PRODUCT_VERIFIED", evidence.cart_id ? {cart_id: evidence.cart_id} : {});
  }
  async function configureProduct(expected) {
    checkExpected(expected);
    const rejected = guard(); if (rejected) return {...rejected, action: "NONE"};
    if (pageStage() === "CONFIGURATION") {
      const evidence = configEvidence(expected);
      // A billing select can trigger merchant JavaScript/POST. Unobserved changes
      // are refused; choose the exact period on the preceding observed link.
      return result(!evidence.error, evidence.error || "PRODUCT_VERIFIED", {action: "NONE"});
    }
    const evidence = configurationLink(expected);
    if (evidence.error) return result(false, evidence.error, {action: "NONE"});
    if (dispatched.has("configure")) return result(false, "ACTION_OUTCOME_UNKNOWN");
    dispatched.add("configure");
    const response = result(true, "PRODUCT_NAVIGATION_DISPATCHED", {action: "NAVIGATED"});
    location.assign(evidence.link.href);
    return response;
  }
  async function selectBillingPeriod(expected) {
    checkExpected(expected);
    const rejected = guard(); if (rejected) return {...rejected, action: "NONE"};
    if (pageStage() !== "PRODUCT" || location.href !== expected.url) return result(false, "PRODUCT_MISMATCH", {action: "NONE"});
    if (expected.period !== "annually") return result(false, "BILLING_PERIOD_UNOBSERVED", {action: "NONE"});
    if (observedLinks(expected, false).length !== 1) return result(false, "SITE_CHANGED", {action: "NONE"});
    const radios = all('input[type="radio"]').filter(node =>
      norm(node.getAttribute("aria-label")) === "1 Year" ||
      [...(node.labels || [])].some(label => visible(label) && text(label) === "1 Year"));
    if (radios.length !== 1 || radios[0].disabled || radios[0].form) return result(false, "BILLING_SELECTOR_CHANGED", {action: "NONE"});
    if (radios[0].checked) return result(true, "BILLING_ALREADY_SELECTED", {action: "NONE"});
    if (dispatched.has("billing")) return result(false, "ACTION_OUTCOME_UNKNOWN");
    dispatched.add("billing");
    // Public catalog view selection only; no form, account fields or purchase
    // submit. The worker must verify the resulting annual link/amount separately.
    radios[0].click();
    return result(true, "BILLING_SELECTION_DISPATCHED", {action: "CONFIGURED"});
  }
  async function addToCart(expected) {
    checkExpected(expected);
    const rejected = guard(true); if (rejected) return {...rejected, action: "NONE"};
    const evidence = configEvidence(expected);
    if (evidence.error) return result(false, evidence.error, {action: "NONE"});
    // This release only handles a single draft. A later index indicates an
    // existing/unknown basket; never POST into it or remove any user items.
    // Index zero alone does not prove that the pre-navigation basket was empty.
    if (evidence.cart_id !== "configuration_0") return result(false, "CART_REVIEW_REQUIRED", {action: "NONE", cart_id: evidence.cart_id});
    if (dispatched.has("cart")) return result(false, "ACTION_OUTCOME_UNKNOWN");
    dispatched.add("cart");
    const response = result(true, "CART_SUBMIT_DISPATCHED", {action: "SUBMITTED", cart_id: evidence.cart_id});
    HTMLFormElement.prototype.requestSubmit.call(evidence.form, evidence.button);
    return response;
  }
  async function verifyCart(expected) {
    checkExpected(expected);
    const rejected = guard(); if (rejected) return rejected;
    const evidence = itemEvidence(expected);
    const buttons = submitControl("Checkout");
    if (!evidence.error && (buttons.length !== 1 || buttons[0].disabled)) return result(false, "CHECKOUT_CONTROL_MISSING");
    return result(!evidence.error, evidence.error || "CART_READY", evidence.cart_id ? {cart_id: evidence.cart_id} : {});
  }
  async function openCheckout(expected) {
    checkExpected(expected);
    const rejected = guard(true); if (rejected) return {...rejected, action: "NONE"};
    const evidence = itemEvidence(expected);
    if (evidence.error) return result(false, evidence.error, {action: "NONE"});
    const buttons = submitControl("Checkout");
    if (buttons.length !== 1 || buttons[0].disabled) return result(false, "CHECKOUT_CONTROL_MISSING", {action: "NONE"});
    if (buttons[0].type !== "button") return result(false, "UNSAFE_CHECKOUT_TYPE", {action: "NONE"});
    // Accept only this previously observed navigation literal. Never evaluate
    // the merchant handler; perform the fixed official GET below instead.
    if (!/^\s*window\.location\s*=\s*(['"])cart\.php\?a=checkout\1\s*;?\s*$/.test(buttons[0].getAttribute("onclick") || "")) return result(false, "CHECKOUT_TARGET_CHANGED", {action: "NONE"});
    if (dispatched.has("checkout")) return result(false, "ACTION_OUTCOME_UNKNOWN");
    dispatched.add("checkout");
    const response = result(true, "CHECKOUT_NAVIGATION_DISPATCHED", {action: "NAVIGATED", cart_id: evidence.cart_id});
    location.assign(ORIGIN + "/cart.php?a=checkout");
    return response;
  }
  async function verifyCheckout(expected) {
    checkExpected(expected);
    const rejected = guard(); if (rejected) return rejected;
    const evidence = itemEvidence(expected, true);
    if (evidence.error) return result(false, evidence.error);
    const buttons = submitControl("Complete Order");
    if (buttons.length !== 1 || buttons[0].type !== "submit" || !buttons[0].form ||
        buttons[0].form.method !== "post" || !route(buttons[0].form.action, "checkout")) return result(false, "SITE_CHANGED");
    if (loginState() !== "VALID") return result(false, "LOGIN_REQUIRED");
    return result(true, "CHECKOUT_READY");
  }
  const api = {
    async detectPage() { return guard() || result(pageStage() !== "UNKNOWN", pageStage() === "UNKNOWN" ? "SITE_CHANGED" : "PAGE_OPENED"); },
    async detectChallenge() { return guard() || result(true, "NO_CHALLENGE"); },
    async detectLogin() { return guard() || result(loginState() === "VALID", loginState() === "VALID" ? "SESSION_VALID" : "LOGIN_REQUIRED"); },
    verifyProduct, selectBillingPeriod, configureProduct, addToCart, verifyCart, openCheckout, verifyCheckout,
    async detectOrder() { return guard() || result(false, "ORDER_UNVERIFIED"); },
    async detectInvoice() { return guard() || result(false, "INVOICE_UNVERIFIED"); },
    async detectPaymentReady() { return guard() || result(false, "PAYMENT_READY_UNVERIFIED"); }
  };
  Object.defineProperty(globalThis, "AutoGrabBandwagon", {value: Object.freeze(api), writable: false, configurable: false});
})();
