// Three bounded Native Messaging round trips on the existing controller/ledger.
// No monitoring ARM, popup action or reconnect path can enter submitOrder.
import {officialInvoiceURL} from "./protocol.js";

const sameProduct = (a, b) => ["name", "url", "period", "cents", "currency"].every(k => a?.[k] === b?.[k]);
const FIELDS = new Set(["stage", "code", "login", "challenge", "outcome", "order_id", "invoice_id", "amount_cents", "currency", "official_url", "unpaid", "observed_at", "created_at", "product_verified", "billing", "no_charge_verified", "scope_complete", "time_window_verified", "submission_nonce"]);
const unknown = code => ({stage: "RECONCILING", code, login: "UNKNOWN", challenge: "UNKNOWN", outcome: "UNKNOWN"});
function evidence(result, product, now) {
  return result?.ok === true && result.login === "VALID" && result.challenge === "NONE" && result.product_verified === true
    && result.amount_cents === product.cents && result.currency === product.currency && result.billing === product.period
    && Number.isFinite(Date.parse(result.observed_at)) && Math.abs(now - Date.parse(result.observed_at)) <= 30000;
}
function observation(result) {
  const output = {};
  for (const [key, value] of Object.entries(result || {})) if (FIELDS.has(key)) output[key] = value;
  output.stage ||= "RECONCILING"; output.code ||= "ORDER_READ_UNVERIFIED";
  output.login ||= "UNKNOWN"; output.challenge ||= "UNKNOWN";
  output.outcome ||= output.challenge === "REQUIRED" ? "HUMAN_ACTION_REQUIRED" : output.login === "REQUIRED" ? "LOGIN_REQUIRED" : "UNKNOWN";
  return output;
}
export async function handleOrderCommand(controller, message) {
  const c = controller, p = message.payload;
  const emit = payload => c.event("ORDER_OBSERVATION", {...payload, tab_id: p.tab_id}, message.command_id, message);
  const refuse = code => { emit(unknown(code)); return {accepted: false, code}; };
  if (c.orderBusy) return refuse("ORDER_COMMAND_BUSY");
  c.orderBusy = true;
  try {
    let a = c.active;
    if (a && !c.matches(message) && (!["CHECKOUT_READY", "CANCELLED"].includes(a.state) || a.order_journal)) return refuse("OTHER_INTENT_ACTIVE");
    if (c.matches(message) && (!sameProduct(a.product,p.product) || a.tab_id !== p.tab_id || a.state === "CANCELLED")) return refuse("ORDER_CONTEXT_MISMATCH");
    if (message.type === "SUBMIT_ORDER" && (!c.matches(message) || !a.order_precheck)) return refuse("ORDER_PRECHECK_REQUIRED");
    if (a?.journal?.status === "DISPATCHED") return refuse("CART_MUTATION_UNCERTAIN");
    if (!c.matches(message)) {
      a = {intent_id: message.intent_id, provider: message.provider, product_id: message.product_id, product: p.product,
        command_id: message.command_id, tab_id: p.tab_id, state: "ORDER_PRECHECK", checkpoint: "CHECKOUT_READY",
        document_id: null, journal: null, challenge: "UNKNOWN", login: "UNKNOWN"};
      c.active = a;
    }
    if (message.type !== "RECONCILE_ORDER" && a.order_journal) return refuse("ORDER_ALREADY_DISPATCHED");
    a.order_session = true; a.command_id = message.command_id;
    c.revision += 1;
    const revision = c.revision, transport = c.port;
    const current = () => c.connected && c.port === transport && c.active === a && c.revision === revision && a.state !== "DISARMED" && a.state !== "CANCELLED";
    await c.save();
    if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
    const tab = await c.api.tabs.get(p.tab_id);
    if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
    const origin = message.provider === "bandwagon" ? "https://bandwagonhost.com" : "https://www.dmit.io";
    let u; try { u = new URL(tab.url); } catch { return refuse("ORDER_PAGE_UNVERIFIED"); }
    if (tab.status === "loading" || u.origin !== origin || u.username || u.password) return refuse("ORDER_PAGE_UNVERIFIED");
    if (message.type !== "RECONCILE_ORDER" && u.href !== origin + "/cart.php?a=checkout") return refuse("CHECKOUT_REQUIRED");

    if (message.type === "ORDER_PRECHECK") {
      a.order_precheck = null;
      const result = await c.read("orderPrecheck", {order_context: {provider: message.provider}});
      if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
      const payload = observation(result);
      if (evidence(result, p.product, c.now()) && result.outcome === "PRECHECK_READY" && result.no_charge_verified === true) {
        a.order_precheck = {id: c.uuid(), document_id: a.document_id, observed_at: c.now(), connection_id: c.connectionId};
        payload.precheck_id = a.order_precheck.id;
      } else if (payload.outcome === "PRECHECK_READY") Object.assign(payload, unknown("ORDER_EVIDENCE_INCOMPLETE"));
      a.state = "ORDER_PRECHECK"; a.login = payload.login; a.challenge = payload.challenge;
      await c.save();
      if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
      emit(payload);
      if (payload.login === "REQUIRED" || payload.challenge === "REQUIRED") await c.focus();
      return {accepted: true};
    }
    if (message.type === "SUBMIT_ORDER") {
      const permit = p.permit, checked = a.order_precheck;
      if (!checked || checked.id !== permit.precheck_id || checked.connection_id !== c.connectionId || c.now() - checked.observed_at > 60000
        || checked.document_id !== a.document_id || c.orderNonces.includes(permit.nonce)) return refuse("ORDER_PRECHECK_STALE");
      const result = await c.read("orderPrecheck", {order_context: {provider: message.provider}});
      if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
      if (a.document_id !== checked.document_id || !evidence(result,p.product,c.now()) || result.outcome !== "PRECHECK_READY" || result.no_charge_verified !== true) {
        a.order_precheck = null; await c.save();
        if (current()) emit(observation(result?.outcome === "PRECHECK_READY" ? unknown("ORDER_PRECHECK_CHANGED") : result));
        return {accepted: false, code: "ORDER_PRECHECK_CHANGED"};
      }
      a.order_journal = {status: "DISPATCHED", nonce: permit.nonce, submitted_at: permit.issued_at, document_id: a.document_id};
      a.order_precheck = null; a.state = "ORDER_SUBMITTING";
      c.orderNonces.push(permit.nonce); c.orderNonces = c.orderNonces.slice(-256);
      // Save failure, disconnect, disarm or expiry here prevents the click.
      // The consumed nonce is never released, including proven no-action results.
      await c.save();
      if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
      if (c.now() >= Date.parse(permit.expires_at)) {
        a.state = "ORDER_UNCERTAIN"; await c.save();
        if (current()) emit({...unknown("ORDER_PERMIT_EXPIRED"), stage: "ORDER_UNCERTAIN", submission_nonce: permit.nonce});
        return {accepted: false, code: "ORDER_PERMIT_EXPIRED"};
      }
      let resultAfter;
      try {
        resultAfter = await c.api.tabs.sendMessage(a.tab_id, {source: "AUTOGRAB_COMPANION", provider: a.provider, operation: "submitOrder",
          expected: c.expected(), ticket: permit.nonce, order_context: {provider: a.provider, permit}}, {documentId: checked.document_id});
      } catch { resultAfter = null; }
      if (!current()) return {accepted: false, code: "ORDER_OUTCOME_UNCERTAIN"};
      if (resultAfter?.action === "NONE") a.order_journal.status = "NOT_APPLIED";
      a.state = "ORDER_UNCERTAIN"; await c.save();
      if (current()) emit({...unknown(resultAfter?.code || "ORDER_OUTCOME_UNCERTAIN"), stage: "ORDER_UNCERTAIN", submission_nonce: permit.nonce,
        login: resultAfter?.login || "UNKNOWN", challenge: resultAfter?.challenge || "UNKNOWN"});
      return {accepted: true};
    }
    if (a.order_journal && (p.submission_nonce !== a.order_journal.nonce || p.submitted_at !== a.order_journal.submitted_at)) return refuse("RECONCILIATION_CONTEXT_MISMATCH");
    const result = await c.read("reconcileOrder", {order_context: {provider: a.provider, submission_nonce: p.submission_nonce, submitted_at: p.submitted_at, order_id: p.order_id, invoice_id: p.invoice_id}});
    if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
    let payload = observation(result);
    if (["ORDER_FOUND", "INVOICE_FOUND", "PAYMENT_READY", "NO_ORDER_FOUND"].includes(payload.outcome) && !evidence(result,p.product,c.now())) payload = unknown("ORDER_EVIDENCE_INCOMPLETE");
    if (["ORDER_FOUND", "INVOICE_FOUND", "PAYMENT_READY"].includes(payload.outcome) &&
        ((!p.order_id && !(Date.parse(payload.created_at) >= Date.parse(p.submitted_at) - 5000)) || p.order_id && p.order_id !== payload.order_id || p.invoice_id && p.invoice_id !== payload.invoice_id)) payload = unknown("RECONCILIATION_IDENTITY_UNVERIFIED");
    if (payload.outcome === "NO_ORDER_FOUND" && (payload.scope_complete !== true || payload.time_window_verified !== true || payload.submission_nonce !== p.submission_nonce)) payload = unknown("RECONCILIATION_SCOPE_UNVERIFIED");
    if (payload.outcome === "PAYMENT_READY") {
      const currentTab = await c.api.tabs.get(a.tab_id);
      if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
      if (currentTab.status === "loading" || currentTab.url !== payload.official_url || !officialInvoiceURL(payload.official_url,a.provider,payload.invoice_id) || payload.unpaid !== true) payload = unknown("PAYMENT_PAGE_UNVERIFIED");
    }
    a.state = payload.outcome === "PAYMENT_READY" ? "PAYMENT_READY" : payload.outcome === "ORDER_FOUND" ? "ORDER_CREATED" : "ORDER_UNCERTAIN";
    if (a.order_journal && ["ORDER_FOUND", "INVOICE_FOUND", "PAYMENT_READY"].includes(payload.outcome)) a.order_journal.status = "VERIFIED";
    a.login = payload.login; a.challenge = payload.challenge;
    await c.save();
    if (!current()) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
    emit(payload);
    if (payload.login === "REQUIRED" || payload.challenge === "REQUIRED") await c.focus();
    if (payload.outcome === "ORDER_FOUND" && officialInvoiceURL(payload.official_url,a.provider,payload.invoice_id) && tab.url !== payload.official_url && current()) {
      // One observed read-only invoice GET, never a guessed URL or payment link.
      await c.api.tabs.update(a.tab_id, {url: payload.official_url});
      // A subsequent explicit read must verify the new invoice document.
    }
    return {accepted: true};
  } catch {
    if (c.active?.order_journal) c.active.state = "ORDER_UNCERTAIN";
    if (c.connected) emit({...unknown("ORDER_READ_OR_DISPATCH_UNCERTAIN"), stage: "ORDER_UNCERTAIN"});
    return {accepted: false, code: "ORDER_READ_OR_DISPATCH_UNCERTAIN"};
  } finally { c.orderBusy = false; }
}
