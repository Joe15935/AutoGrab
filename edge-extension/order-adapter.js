// Shared experimental order/receipt DOM contract. No real order page has yet
// validated these selectors. Missing evidence always closes the submit gate.
(() => {
  if (globalThis.AutoGrabOrders) return;
  const ORIGINS = {bandwagon: "https://bandwagonhost.com", dmit: "https://www.dmit.io"};
  const ADAPTERS = {bandwagon: "AutoGrabBandwagon", dmit: "AutoGrabDMIT"};
  const PERIODS = {monthly: "Monthly", quarterly: "Quarterly", semiannually: "Semiannually", annually: "Annually", biennially: "Biennially", triennially: "Triennially"};
  const ID = /^[1-9][0-9]{0,39}$/;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const consumed = new Set();
  const visible = n => !!n && n.getClientRects().length > 0 && getComputedStyle(n).visibility !== "hidden";
  const text = n => (n?.innerText || "").replace(/\s+/g, " ").trim();
  const one = (selector, root = document) => { const nodes = [...root.querySelectorAll(selector)].filter(visible); return nodes.length === 1 ? nodes[0] : null; };
  const integer = value => Number.isSafeInteger(value) && value > 0 && value < 1e12;
  function valid(expected, context) {
    return context && Object.hasOwn(ORIGINS, context.provider) && window.top === window && location.origin === ORIGINS[context.provider]
      && expected && Object.keys(expected).sort().join(",") === "cents,currency,name,period,product_id,url"
      && ID.test(expected.product_id) && typeof expected.name === "string" && expected.name.length > 0 && expected.name.length <= 200
      && Object.hasOwn(PERIODS, expected.period) && expected.currency === "USD" && integer(expected.cents);
  }
  function safetyNow(context) {
    const surface = document.title + " " + [...document.querySelectorAll('h1,h2,h3,[role="alert"]')].filter(visible).map(n=>text(n).slice(0,1000)).join(" ");
    const challenged = /verify you are human|checking your browser|just a moment|确认您是真人|验证您是真人|请稍候/i.test(surface)
      || [...document.querySelectorAll('#challenge-running,#challenge-stage,.cf-turnstile')].some(visible);
    if (challenged) return {login: "UNKNOWN", challenge: "REQUIRED"};
    // Presence, never values, of login/payment input fields is inspected.
    const login = [...document.querySelectorAll('input[type="password"]')].some(visible) ? "REQUIRED" :
      [...document.querySelectorAll('a[href]')].some(a => {
        try { return visible(a) && new URL(a.getAttribute("href"), location.href).href === ORIGINS[context.provider] + "/logout.php"; } catch { return false; }
      }) ? "VALID" : "UNKNOWN";
    return {login, challenge: "NONE"};
  }
  async function safety(context) {
    const challenge = await globalThis[ADAPTERS[context.provider]]?.detectChallenge();
    if (challenge?.challenge !== "NONE") return {login: "UNKNOWN", challenge: challenge?.challenge || "UNKNOWN"};
    return safetyNow(context);
  }
  function output(safe, code, extra = {}) {
    const outcome = safe.challenge === "REQUIRED" ? "HUMAN_ACTION_REQUIRED" : safe.login === "REQUIRED" ? "LOGIN_REQUIRED" : "UNKNOWN";
    return {ok: false, stage: "ORDER_PRECHECK", code, outcome, ...safe, action: "NONE", ...extra};
  }
  function money(value, expected) {
    const match = /^\$([0-9]+(?:,[0-9]{3})*)\.([0-9]{2}) USD$/.exec(value);
    return !!match && Number(match[1].replaceAll(",", "")) * 100 + Number(match[2]) === expected.cents;
  }
  function identity(scope, expected) {
    if (!scope) return false;
    const rows = [...scope.querySelectorAll('tr[data-product-id], .order-item[data-product-id]')].filter(visible);
    if (rows.length !== 1 || rows[0].getAttribute("data-product-id") !== expected.product_id) return false;
    const row = rows[0], quantity = row.getAttribute("data-quantity");
    return (!quantity || quantity === "1") && text(one('.product-name,[data-role="product-name"]', row)) === expected.name
      && text(one('.billing-cycle,[data-role="billing"]', row)) === PERIODS[expected.period]
      && money(text(one('.item-amount,[data-role="amount"]', row)), expected)
      && money(text(one('#totalDueToday,[data-role="total"]', scope)), expected);
  }
  function submitBoundary() {
    const forms = [...document.querySelectorAll('form[method="post" i]')].filter(form => {
      try { const u = new URL(form.getAttribute("action"), location.href); return visible(form) && u.href === location.origin + "/cart.php?a=checkout"; } catch { return false; }
    });
    if (forms.length !== 1) return null;
    const form = forms[0], buttons = [...form.querySelectorAll('button[type="submit"],input[type="submit"]')].filter(visible);
    if (buttons.length !== 1) return null;
    const button = buttons[0], label = button.tagName === "INPUT" ? button.value : text(button);
    if (label !== "Complete Order" || button.form !== form || button.disabled || button.getAttribute("aria-disabled") === "true"
      || ["formaction", "formmethod", "formtarget", "formenctype"].some(name=>button.hasAttribute(name)) || form.hasAttribute("target")) return null;
    return {form, button};
  }
  function noCharge(boundary) {
    if (!boundary) return false;
    const form = boundary.form;
    // Experimental, explicit merchant evidence is required. An absent balance,
    // card or automatic-payment control proves nothing about server behavior.
    if (text(one('#orderPaymentNotice', form)) !== "Placing this order only creates an unpaid invoice. No payment will be taken or credit applied.") return false;
    if (text(one('#accountCreditBalance', form)) !== "$0.00 USD" || text(one('#savedPaymentMethods', form)) !== "No saved payment methods"
      || text(one('#automaticPayments', form)) !== "Automatic payments disabled") return false;
    const controls = [...form.elements];
    if (controls.some(n=>n.form !== form || !form.contains(n))) return false;
    const paymentControls = controls.filter(n=>n.name === "paymentmethod");
    if (paymentControls.some(n=>n.tagName !== "INPUT" || n.type !== "radio" || !visible(n) || n.checked && n.disabled)) return false;
    const methods = paymentControls.filter(n=>n.checked && !n.disabled);
    if (methods.length !== 1 || !["banktransfer", "mailin"].includes(methods[0].value)) return false;
    if (form.querySelector('input[autocomplete^="cc-"],input[name="ccnumber"],iframe[src*="stripe"],iframe[src*="paypal"]')) return false;
    if (controls.filter(n=>["applycredit", "usecredit", "autopay"].includes(n.name)).some(n=>n.type !== "checkbox" || n.checked || n.disabled || !visible(n))) return false;
    const terms = [...form.querySelectorAll('input[name="accepttos"]')];
    return terms.length === 1 && terms[0].checked && !terms[0].disabled && visible(terms[0]);
  }
  async function precheck(expected, context) {
    if (!valid(expected, context)) return output({login: "UNKNOWN", challenge: "UNKNOWN"}, "ORDER_CONTEXT_INVALID");
    const safe = await safety(context);
    if (safe.login !== "VALID" || safe.challenge !== "NONE") return output(safe, "ORDER_SESSION_UNVERIFIED");
    if (location.pathname !== "/cart.php" || location.search !== "?a=checkout") return output(safe, "CHECKOUT_REQUIRED");
    const boundary = submitBoundary(), summary = one('#orderSummary');
    if (!identity(summary, expected)) return output(safe, "ORDER_PRODUCT_UNVERIFIED", {product_verified: false});
    if (!noCharge(boundary)) return output(safe, "NO_CHARGE_UNVERIFIED", {product_verified: true, no_charge_verified: false});
    return output(safe, "ORDER_PRECHECK_VERIFIED", {ok: true, outcome: "PRECHECK_READY", product_verified: true, no_charge_verified: true,
      amount_cents: expected.cents, currency: expected.currency, billing: expected.period, observed_at: new Date().toISOString()});
  }
  async function submitOrder(expected, context) {
    const permit = context?.permit, now = Date.now(), issued = Date.parse(permit?.issued_at), expires = Date.parse(permit?.expires_at);
    if (!permit || permit.real_order_smoke_test_armed !== true || !UUID.test(permit.nonce) || !UUID.test(permit.precheck_id)
      || !Number.isFinite(issued) || !Number.isFinite(expires) || issued > now || expires <= now || expires - issued > 60000 || expires <= issued || consumed.has(permit.nonce)) {
      return output({login: "UNKNOWN", challenge: "UNKNOWN"}, "ORDER_PERMIT_REJECTED");
    }
    const checked = await precheck(expected, context);
    if (!checked.ok) return checked;
    // The worker has already persisted ORDER_SUBMITTING and consumed this nonce.
    // There is exactly one ordinary order click and no payment method action.
    const boundary = submitBoundary(), fresh = safetyNow(context);
    if (!valid(expected,context) || location.pathname !== "/cart.php" || location.search !== "?a=checkout" || fresh.login !== "VALID" || fresh.challenge !== "NONE"
      || !identity(one('#orderSummary'),expected) || !boundary || !noCharge(boundary) || Date.now() >= expires) return output(fresh, "ORDER_PRECHECK_CHANGED");
    consumed.add(permit.nonce);
    boundary.button.click();
    return {ok: false, stage: "ORDER_UNCERTAIN", code: "ORDER_DISPATCHED", outcome: "UNKNOWN", login: checked.login, challenge: checked.challenge, action: "SUBMITTED"};
  }
  function invoiceURL(value, provider, id) {
    try { const u = new URL(value, location.href); return ID.test(id) && u.href === ORIGINS[provider] + "/viewinvoice.php?id=" + id ? u.href : null; } catch { return null; }
  }
  function inScope(root, context) {
    const created = one('time[data-role="created-at"][datetime]', root), then = Date.parse(created?.getAttribute("datetime")), submitted = Date.parse(context.submitted_at);
    return UUID.test(context.submission_nonce) && Number.isFinite(submitted) && submitted <= Date.now() && Number.isFinite(then) && then >= submitted - 5000 && then <= Date.now();
  }
  async function reconcileOrder(expected, context) {
    if (!valid(expected, context)) return output({login: "UNKNOWN", challenge: "UNKNOWN"}, "ORDER_CONTEXT_INVALID", {stage: "RECONCILING"});
    const safe = await safety(context);
    if (safe.login !== "VALID" || safe.challenge !== "NONE") return output(safe, "ORDER_SESSION_UNVERIFIED", {stage: "RECONCILING"});
    const invoice = one('#invoice[data-order-id][data-invoice-id]'), order = one('#orderConfirmation[data-order-id]');
    const root = invoice || order;
    if (!root || !inScope(root, context) || !identity(root, expected)) return output(safe, "RECONCILIATION_SCOPE_UNVERIFIED", {stage: "RECONCILING"});
    const orderId = root.getAttribute("data-order-id"), invoiceId = invoice?.getAttribute("data-invoice-id") || root.getAttribute("data-invoice-id");
    if (!ID.test(orderId) || context.order_id && context.order_id !== orderId || context.invoice_id && context.invoice_id !== invoiceId) return output(safe, "ORDER_ID_UNVERIFIED", {stage: "RECONCILING"});
    const fields = {ok: true, stage: "ORDER_CREATED", code: "ORDER_FOUND", outcome: "ORDER_FOUND", order_id: orderId,
      product_verified: true, amount_cents: expected.cents, currency: expected.currency, billing: expected.period, observed_at: new Date().toISOString(),
      created_at: new Date(Date.parse(one('time[data-role="created-at"][datetime]', root).getAttribute("datetime"))).toISOString()};
    if (!invoice) {
      const links = [...root.querySelectorAll('a[href]')].filter(a => visible(a) && invoiceURL(a.getAttribute("href"), context.provider, invoiceId));
      if (links.length === 1) Object.assign(fields, {invoice_id: invoiceId, official_url: invoiceURL(links[0].getAttribute("href"), context.provider, invoiceId)});
      return output(safe, "ORDER_FOUND", fields);
    }
    const url = invoiceURL(location.href, context.provider, invoiceId), status = text(one('[data-role="invoice-status"]', invoice));
    const pay = [...invoice.querySelectorAll('button,input[type="submit"],a[href]')].filter(n => visible(n) && /^(Pay Now|Make Payment|Proceed to Payment)$/.test(n.tagName === "INPUT" ? n.value : text(n)));
    if (!url || status !== "Unpaid" || pay.length !== 1) return output(safe, "UNPAID_PAYMENT_PAGE_UNVERIFIED", {stage: "PAYMENT_LINK_SEARCHING"});
    return output(safe, "PAYMENT_READY_VERIFIED", {...fields, stage: "PAYMENT_READY", code: "PAYMENT_READY_VERIFIED", outcome: "PAYMENT_READY", invoice_id: invoiceId, official_url: url, unpaid: true});
  }
  Object.defineProperty(globalThis, "AutoGrabOrders", {value: Object.freeze({experimental: true, precheck, submitOrder, reconcileOrder})});
})();
