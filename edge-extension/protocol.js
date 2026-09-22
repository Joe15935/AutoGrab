// Fixed, versioned Native Messaging protocol. No arbitrary selectors or scripts.
export const VERSION = 1;
export const EXTENSION_VERSION = "0.5.0";
export const HOST = "com.autograb.edge";
export const MAX_BYTES = 65536;
export const ORDER_COMMANDS = new Set(["ORDER_PRECHECK", "SUBMIT_ORDER", "RECONCILE_ORDER"]);
export const COMMANDS = new Set(["PING", "GET_STATUS", "DISARM", "OPEN_PRODUCT", "START_DRY_RUN", "START_CHECKOUT", "RESUME_INTENT", "CANCEL_INTENT", ...ORDER_COMMANDS]);
export const EVENTS = new Set(["EDGE_READY", "PAGE_OPENED", "PRODUCT_VERIFIED", "CART_READY", "CHECKOUT_READY", "ORDER_CREATED", "INVOICE_FOUND", "PAYMENT_READY", "LOGIN_REQUIRED", "HUMAN_CHALLENGE_REQUIRED", "SOLD_OUT", "SITE_CHANGED", "FAILED", "ORDER_OBSERVATION"]);
const CONTROLS = new Set(["PING", "GET_STATUS", "DISARM", "EDGE_READY"]);
export const PROVIDERS = new Set(["bandwagon", "dmit", "vmiss", "vps", "apple"]);
const PERIODS = ["monthly", "quarterly", "semiannually", "annually", "biennially", "triennially", "one_time", "unknown"];
const CURRENCIES = ["USD", "EUR", "CNY", "HKD", "CAD", "GBP", null];
const APPLE_REGIONS = {cn: ["www.apple.com.cn", ""], us: ["www.apple.com", ""], hk: ["www.apple.com", "/hk-zh"], tw: ["www.apple.com", "/tw"], jp: ["www.apple.com", "/jp"], sg: ["www.apple.com", "/sg"], au: ["www.apple.com", "/au"], my: ["www.apple.com", "/my"]};
const ORIGINS = {bandwagon: ["https://bandwagonhost.com"], dmit: ["https://www.dmit.io"], vmiss: ["https://app.vmiss.com"], vps: ["https://v.ps", "https://vps.hosting"], apple: ["https://www.apple.com", "https://www.apple.com.cn"]};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const IDENTIFIER = /^[A-Z][A-Z0-9_]{0,63}$/;
const EVENT_FIELDS = new Set(["version", "tab_id", "url", "stage", "code", "login", "challenge", "cart_id", "resume_from", "mutation_uncertain"]);
const ORDER_FIELDS = new Set(["tab_id", "stage", "code", "login", "challenge", "outcome", "precheck_id", "order_id", "invoice_id", "amount_cents", "currency", "official_url", "unpaid", "observed_at", "created_at", "submission_nonce", "product_verified", "billing", "no_charge_verified", "scope_complete", "time_window_verified"]);
const ORDER_OUTCOMES = new Set(["PRECHECK_READY", "ORDER_FOUND", "INVOICE_FOUND", "PAYMENT_READY", "NO_ORDER_FOUND", "UNKNOWN", "LOGIN_REQUIRED", "HUMAN_ACTION_REQUIRED"]);
const utcTime = value => typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value));
const object = value => value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
const keysAre = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
function assert(condition, code = "INVALID_MESSAGE") { if (!condition) throw new Error(code); }
export function officialInvoiceURL(value, provider, invoiceId) {
  return ["bandwagon", "dmit"].includes(provider) && typeof invoiceId === "string" && /^[1-9][0-9]{0,39}$/.test(invoiceId)
    && value === ORIGINS[provider][0] + "/viewinvoice.php?id=" + invoiceId;
}
export function validateOrderPayload(message, direction, now = Date.now()) {
  const p = message.payload;
  assert(["bandwagon", "dmit"].includes(message.provider), "ORDER_PROVIDER_UNSUPPORTED");
  const tab = value => Number.isSafeInteger(value) && value >= 0 && value < 2147483648;
  if (direction === "command") {
    const fields = ["product", "mode", "tab_id", ...(message.type === "SUBMIT_ORDER" ? ["permit"] : message.type === "RECONCILE_ORDER" ? ["submission_nonce", "submitted_at", "order_id", "invoice_id"] : [])];
    assert(keysAre(p, fields) && tab(p.tab_id));
    validateProduct(p.product, message.provider, message.product_id, message.type);
    assert(p.product.currency === "USD" && PERIODS.slice(0, 6).includes(p.product.period), "ORDER_PRODUCT_UNSUPPORTED");
    assert(p.mode === (message.type === "SUBMIT_ORDER" ? "REAL_ORDER_SMOKE_TEST" : "DRY_RUN"), "INVALID_ORDER_MODE");
    if (message.type === "SUBMIT_ORDER") {
      const permit = p.permit;
      assert(keysAre(permit, ["nonce", "issued_at", "expires_at", "precheck_id", "real_order_smoke_test_armed"]), "ORDER_PERMIT_REJECTED");
      assert(UUID.test(permit.nonce) && UUID.test(permit.precheck_id) && permit.real_order_smoke_test_armed === true && utcTime(permit.issued_at) && utcTime(permit.expires_at), "ORDER_PERMIT_REJECTED");
      const issued = Date.parse(permit.issued_at), expires = Date.parse(permit.expires_at);
      assert(issued <= now && expires > now && expires > issued && expires - issued <= 60000, "ORDER_PERMIT_EXPIRED");
    }
    if (message.type === "RECONCILE_ORDER") {
      assert(p.submission_nonce === null && p.submitted_at === null || UUID.test(p.submission_nonce) && utcTime(p.submitted_at) && Date.parse(p.submitted_at) <= now, "INVALID_RECONCILIATION_CONTEXT");
      for (const key of ["order_id", "invoice_id"]) assert(p[key] === null || typeof p[key] === "string" && /^[1-9][0-9]{0,39}$/.test(p[key]), "INVALID_RECONCILIATION_CONTEXT");
      assert(p.invoice_id === null || p.order_id !== null, "INVALID_RECONCILIATION_CONTEXT");
    }
    return;
  }
  assert(Object.keys(p).every(k => ORDER_FIELDS.has(k)) && ["tab_id", "stage", "code", "login", "challenge", "outcome"].every(k => Object.hasOwn(p, k)));
  assert(tab(p.tab_id) && IDENTIFIER.test(p.stage) && IDENTIFIER.test(p.code) && ["VALID", "REQUIRED", "UNKNOWN"].includes(p.login) && ["NONE", "REQUIRED", "UNKNOWN"].includes(p.challenge) && ORDER_OUTCOMES.has(p.outcome));
  for (const key of ["precheck_id", "submission_nonce"]) if (Object.hasOwn(p,key)) assert(UUID.test(p[key]));
  for (const key of ["order_id", "invoice_id"]) if (Object.hasOwn(p,key)) assert(typeof p[key] === "string" && /^[1-9][0-9]{0,39}$/.test(p[key]));
  for (const key of ["product_verified", "no_charge_verified", "unpaid", "scope_complete", "time_window_verified"]) if (Object.hasOwn(p,key)) assert(typeof p[key] === "boolean");
  if (Object.hasOwn(p,"amount_cents")) assert(Number.isSafeInteger(p.amount_cents) && p.amount_cents > 0 && p.amount_cents < 1e12);
  if (Object.hasOwn(p,"currency")) assert(p.currency === "USD");
  if (Object.hasOwn(p,"billing")) assert(PERIODS.slice(0,6).includes(p.billing));
  if (Object.hasOwn(p,"observed_at")) assert(utcTime(p.observed_at) && Math.abs(now-Date.parse(p.observed_at)) <= 120000);
  if (Object.hasOwn(p,"created_at")) assert(utcTime(p.created_at) && utcTime(p.observed_at) && Date.parse(p.created_at) <= Date.parse(p.observed_at));
  if (Object.hasOwn(p,"official_url")) assert(officialInvoiceURL(p.official_url,message.provider,p.invoice_id), "INVALID_INVOICE_URL");
  if (["PRECHECK_READY", "ORDER_FOUND", "INVOICE_FOUND", "PAYMENT_READY", "NO_ORDER_FOUND"].includes(p.outcome)) {
    assert(p.login === "VALID" && p.challenge === "NONE" && p.product_verified === true && Object.hasOwn(p,"amount_cents") && p.currency === "USD" && PERIODS.slice(0,6).includes(p.billing) && utcTime(p.observed_at), "ORDER_EVIDENCE_INCOMPLETE");
  }
  if (p.outcome === "PRECHECK_READY") assert(UUID.test(p.precheck_id) && p.no_charge_verified === true, "ORDER_EVIDENCE_INCOMPLETE");
  if (["ORDER_FOUND", "INVOICE_FOUND", "PAYMENT_READY"].includes(p.outcome)) assert(Object.hasOwn(p,"order_id"), "ORDER_EVIDENCE_INCOMPLETE");
  if (["INVOICE_FOUND", "PAYMENT_READY"].includes(p.outcome)) assert(Object.hasOwn(p,"invoice_id"), "ORDER_EVIDENCE_INCOMPLETE");
  if (p.outcome === "PAYMENT_READY") assert(p.unpaid === true && officialInvoiceURL(p.official_url,message.provider,p.invoice_id), "ORDER_EVIDENCE_INCOMPLETE");
  if (p.outcome === "NO_ORDER_FOUND") assert(p.scope_complete === true && p.time_window_verified === true && UUID.test(p.submission_nonce) && !["order_id", "invoice_id", "official_url"].some(k=>Object.hasOwn(p,k)), "ORDER_EVIDENCE_INCOMPLETE");
}
export function validProductId(provider, value) {
  return typeof value === "string" && PROVIDERS.has(provider) && (provider === "apple" ? /^[a-z]{2}:[A-Z0-9]{1,30}\/A$/ : provider === "vmiss" ? /^[a-z0-9][a-z0-9-]{0,79}\/[a-z0-9][a-z0-9-]{0,79}$/ : /^[1-9][0-9]{0,39}$/).test(value);
}
export function allowedOrigin(value, provider = "bandwagon") {
  try { const u = new URL(value); return !u.username && !u.password && (ORIGINS[provider] || []).includes(u.origin); } catch { return false; }
}
function query(u) {
  if (u.search && u.search.slice(1).split("&").some(x => !x.includes("="))) throw new Error("INVALID_QUERY");
  const result = {};
  for (const [key, value] of u.searchParams) { if (Object.hasOwn(result, key)) throw new Error("DUPLICATE_QUERY"); result[key] = value; }
  return result;
}
export function isProductURL(value, provider = "bandwagon", productId = null) {
  try {
    if (typeof value !== "string" || value.length > 2048 || /[^\x21-\x7e]/.test(value)) return false;
    const u = new URL(value), decoded = decodeURIComponent(u.pathname);
    if (value !== `${u.origin}${u.pathname}${u.search}` || !allowedOrigin(value, provider) || u.hash || decoded.split("/").includes("..") || /[\\%\u0000-\u001f\u007f]/.test(decoded)) return false;
    if (provider === "dmit" || provider === "vmiss") {
      const q = query(u), numeric = s => /^[1-9][0-9]{0,39}$/.test(s || "");
      let valid = /^\/store(?:\/[a-z0-9][a-z0-9-]{0,79}){0,2}\/?$/.test(u.pathname) && !u.search;
      if (u.pathname === "/cart.php") {
        valid = !Object.keys(q).length || (!Object.hasOwn(q, "a") || q.a === "add" && numeric(q.pid)) && Object.entries(q).every(([k, v]) => {
          if (/^configoption\[[0-9]+\]$/.test(k)) return Object.hasOwn(q, "a") && numeric(v);
          if (![...(Object.hasOwn(q, "a") ? ["a", "pid", "billingcycle"] : ["gid"]), "currency", "language"].includes(k)) return false;
          if (["pid", "gid", "currency"].includes(k)) return numeric(v);
          if (k === "language") return v === "english";
          if (k === "billingcycle") return PERIODS.slice(0, 6).includes(v);
          return true;
        });
      }
      if (productId && q.pid && q.pid !== productId) valid = false;
      if (provider === "vmiss" && productId) valid = valid && [`/store/${productId}`, `/store/${productId.split("/")[0]}`].includes(u.pathname.replace(/\/$/, ""));
      return valid;
    }
    if (provider === "vps") {
      if (u.origin === "https://v.ps") return /^\/products\/[a-z0-9-]{1,80}\/?$/.test(u.pathname) && !u.search;
      const q = query(u);
      return u.pathname === "/" && Object.keys(q).length === 3 && q.cmd === "cart" && q.action === "add" && /^[1-9][0-9]{0,39}$/.test(q.id || "") && (!productId || q.id === productId);
    }
    if (provider === "apple") {
      if (u.search) return false;
      const candidates = productId ? [[productId.split(":")[0], APPLE_REGIONS[productId.split(":")[0]]]] : Object.entries(APPLE_REGIONS);
      return candidates.some(([, location]) => {
        if (!location || u.hostname !== location[0] || !decoded.startsWith(location[1] + "/shop/")) return false;
        const path = decoded.slice(location[1].length), match = /^\/shop\/(?:product\/([A-Za-z0-9]{1,30}\/[Aa])(?:\/[a-zA-Z0-9-]+)?|buy-[a-z0-9-]+(?:\/[a-zA-Z0-9-]+)*)\/?$/.exec(path);
        if (!match) return false;
        const sku = match[1] || (/\/a\/?$/i.test(path) ? path.replace(/\/$/, "").split("/").slice(-2).join("/") : null);
        return !productId || !sku || sku.toUpperCase() === productId.split(":")[1];
      });
    }
    return value === `${u.origin}${u.pathname}` && value.startsWith("https://bandwagonhost.com/") && u.origin === "https://bandwagonhost.com" && !u.username && !u.password && !u.search && !u.hash && !decoded.split("/").includes("..") && !/[\\%\u0000-\u001f\u007f]/.test(decoded) && /^\/order\/ecommerce(?:\/[^/?#]+\/[^/?#]+)?\/?$/.test(u.pathname);
  } catch { return false; }
}
export function sanitizeURL(value, provider = "bandwagon") {
  try {
    const u = new URL(value);
    if (!allowedOrigin(value, provider)) return null;
    if (provider !== "bandwagon") {
      if (provider === "apple") {
        if (isProductURL(`${u.origin}${u.pathname}`, provider)) return `${u.origin}${u.pathname}`;
        const regional = Object.values(APPLE_REGIONS).find(([host, path]) => host === u.hostname && new RegExp(`^${path}/shop/bag/?$`).test(u.pathname));
        return regional ? `${u.origin}${u.pathname}` : `${u.origin}/`;
      }
      if (provider === "vps") {
        if (u.origin === "https://v.ps") return isProductURL(`${u.origin}${u.pathname}`, provider) ? `${u.origin}${u.pathname}` : `${u.origin}/`;
        if (/^\/cart\/[a-z0-9-]{1,80}\/?$/.test(u.pathname)) return `${u.origin}${u.pathname}`;
        const action = u.searchParams.get("action");
        if (u.pathname === "/" && u.searchParams.get("cmd") === "cart" && !action) return `${u.origin}/?cmd=cart`;
        return u.pathname === "/" && u.searchParams.get("cmd") === "cart" && ["view", "checkout"].includes(action) ? `${u.origin}/?cmd=cart&action=${action}` : `${u.origin}/`;
      }
      if (/^\/store(?:\/[a-z0-9][a-z0-9-]{0,79}){0,2}\/?$/.test(u.pathname)) return `${u.origin}${u.pathname}`;
      if (["/clientarea.php", "/index.php", "/"].includes(u.pathname)) return `${u.origin}${u.pathname}`;
      if (u.pathname === "/cart.php") { const a = u.searchParams.get("a"); return `${u.origin}/cart.php${["view", "checkout", "confproduct"].includes(a) ? `?a=${a}` : ""}`; }
      return `${u.origin}/`;
    }
    if (isProductURL(`${u.origin}${u.pathname}`)) return `${u.origin}${u.pathname}`;
    if (["/clientarea.php", "/index.php", "/"].includes(u.pathname)) return `${u.origin}${u.pathname}`;
    if (u.pathname === "/cart.php") {
      const action = u.searchParams.get("a");
      return `${u.origin}/cart.php${["view", "checkout", "confproduct"].includes(action) ? `?a=${action}` : ""}`;
    }
    // Order/invoice/account identifiers are not sent through status telemetry.
    return `${u.origin}/`;
  } catch { return null; }
}
export function validateProduct(product, provider = "bandwagon", productId = null, command = "START_DRY_RUN") {
  assert(keysAre(product, ["name", "url", "period", "cents", "currency"]), "INVALID_PRODUCT");
  assert(typeof product.name === "string" && product.name.length > 0 && product.name.length <= 200 && !/[\u0000-\u001f\u007f]/.test(product.name), "INVALID_PRODUCT");
  assert(isProductURL(product.url, provider, productId), "INVALID_PRODUCT_URL");
  assert((provider === "bandwagon" ? ["annually", "biennially"] : PERIODS).includes(product.period), "INVALID_PRODUCT_PERIOD");
  assert((Number.isSafeInteger(product.cents) && product.cents > 0 && product.cents < 1000000000000) || (provider !== "bandwagon" && product.cents === null), "INVALID_PRODUCT_PRICE");
  assert(provider === "bandwagon" ? product.currency === "USD" : CURRENCIES.includes(product.currency), "INVALID_PRODUCT_CURRENCY");
  assert(command === "OPEN_PRODUCT" || product.cents !== null && product.currency !== null && product.period !== "unknown", "VERIFIED_PRICE_REQUIRED");
  if (command === "OPEN_PRODUCT" && (product.cents === null || product.currency === null || product.period === "unknown")) {
    const u = new URL(product.url);
    assert(u.searchParams.get("a") !== "add" && u.searchParams.get("action") !== "add", "READ_ONLY_PRODUCT_URL_REQUIRED");
  }
  return product;
}
export function validateEnvelope(message, direction = "command", now = Date.now()) {
  assert(keysAre(message, ["version", "type", "message_id", "command_id", "intent_id", "provider", "product_id", "timestamp", "payload"]));
  assert(message.version === VERSION && (direction === "command" ? COMMANDS : EVENTS).has(message.type));
  assert(UUID.test(message.message_id) && UUID.test(message.command_id));
  assert(PROVIDERS.has(message.provider));
  const control = CONTROLS.has(message.type) || (message.type === "PAGE_OPENED" && message.intent_id === null);
  assert(control ? message.intent_id === null && message.product_id === null : UUID.test(message.intent_id) && validProductId(message.provider, message.product_id), "INVALID_IDENTITY");
  assert(!CONTROLS.has(message.type) || message.provider === "bandwagon", "INVALID_IDENTITY");
  assert(typeof message.timestamp === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(message.timestamp));
  const time = Date.parse(message.timestamp);
  assert(Number.isFinite(time) && Math.abs(now - time) <= 120000, "STALE_MESSAGE");
  assert(object(message.payload));
  if (direction === "command") {
    if (ORDER_COMMANDS.has(message.type)) validateOrderPayload(message, direction, now);
    else if (["OPEN_PRODUCT", "START_DRY_RUN", "START_CHECKOUT", "RESUME_INTENT"].includes(message.type)) {
      assert(keysAre(message.payload, ["product", "mode"]) && message.payload.mode === "DRY_RUN"); validateProduct(message.payload.product, message.provider, message.product_id, message.type);
    } else assert(Object.keys(message.payload).length === 0);
  } else if (message.type === "ORDER_OBSERVATION") {
    validateOrderPayload(message, direction, now);
  } else {
    assert(Object.keys(message.payload).every(key => EVENT_FIELDS.has(key)));
    for (const [key, value] of Object.entries(message.payload)) {
      if (key === "version") assert(typeof value === "string" && /^\d{1,4}\.\d{1,4}\.\d{1,4}$/.test(value));
      else if (key === "tab_id") assert(Number.isSafeInteger(value) && value >= 0 && value < 2147483648);
      else if (key === "url") assert(typeof value === "string" && sanitizeURL(value, message.provider) === value);
      else if (["stage", "code", "resume_from"].includes(key)) assert(typeof value === "string" && IDENTIFIER.test(value));
      else if (key === "login") assert(["VALID", "REQUIRED", "UNKNOWN"].includes(value));
      else if (key === "challenge") assert(["NONE", "REQUIRED", "UNKNOWN"].includes(value));
      else if (key === "cart_id") assert(typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$/.test(value));
      else if (key === "mutation_uncertain") assert(typeof value === "boolean");
    }
    if (message.type === "EDGE_READY") assert(Object.hasOwn(message.payload, "version"));
  }
  assert(new TextEncoder().encode(JSON.stringify(message)).length <= MAX_BYTES, "MESSAGE_TOO_LARGE");
  return message;
}
export function envelope(type, identity, payload, commandId, uuid = () => crypto.randomUUID(), now = Date.now()) {
  const message = {version: VERSION, type, message_id: uuid(), command_id: commandId, intent_id: identity?.intent_id ?? null, provider: identity?.provider ?? "bandwagon", product_id: identity?.product_id ?? null, timestamp: new Date(now).toISOString(), payload};
  return validateEnvelope(message, "event", now);
}
