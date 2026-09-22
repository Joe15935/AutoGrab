import {EXTENSION_VERSION, HOST, allowedOrigin, envelope, sanitizeURL, validateEnvelope} from "./protocol.js";

const STORAGE_KEY = "autograbCompanionV1";
const sameProduct = (left, right) => ["name", "url", "period", "cents", "currency"].every(key => left[key] === right[key]);
const TERMINAL = new Set(["CHECKOUT_READY", "CANCELLED"]);
const MAX_CART_REBUILDS = 2;
const MUTATIONS = {
  SELECT_PUBLIC_BILLING: {operation: "selectBillingPeriod", target: "PRODUCT", verify: "verifyProduct", checkpoint: "PUBLIC_BILLING_SELECTED"},
  OPEN_CONFIGURATION: {operation: "configureProduct", target: "CONFIGURATION", verify: "verifyProduct", checkpoint: "CONFIGURATION_OPENED"},
  CONFIGURE_PRODUCT: {operation: "configureProduct", target: "CONFIGURATION", verify: "verifyProduct", checkpoint: "CONFIGURED"},
  ADD_TO_CART: {operation: "addToCart", target: "CART", verify: "verifyCart", checkpoint: "CART_READY"},
  OPEN_CHECKOUT: {operation: "openCheckout", target: "CHECKOUT", verify: "verifyCheckout", checkpoint: "CHECKOUT_READY"},
};

// Dependencies are explicit so offline tests exercise the same durable controller.
export class CompanionController {
  constructor(api, options = {}) {
    this.api = api; this.now = options.now || Date.now; this.uuid = options.uuid || (() => crypto.randomUUID());
    this.active = null; this.port = null; this.connected = false; this.connectionId = null;
    this.lastHeartbeat = null; this.lastError = null; this.seen = []; this.pumping = false; this.revision = 0;
    this.normalObserved = false;
  }
  async restore() {
    const saved = (await this.api.storage.local.get(STORAGE_KEY))[STORAGE_KEY];
    if (saved?.version === 1) {
      this.seen = Array.isArray(saved.seen) ? saved.seen.slice(-256) : [];
      this.active = saved.active || null;
      if (this.active && !this.active.provider) this.active.provider = "bandwagon";
      if (this.active && !TERMINAL.has(this.active.state)) this.active.state = "PAUSED_RESTART";
      await this.save();
    }
  }
  async save() {
    await this.api.storage.local.set({[STORAGE_KEY]: {version: 1, active: this.active, seen: this.seen}});
  }
  status() {
    const a = this.active;
    return {installed: true, connected: this.connected, version: EXTENSION_VERSION, last_heartbeat: this.lastHeartbeat, tab_id: a?.tab_id ?? null, intent_id: a?.intent_id ?? null, provider: a?.provider || "bandwagon", product_id: a?.product_id ?? null, state: a?.state || "IDLE", checkpoint: a?.checkpoint || "NONE", challenge: a?.challenge || "UNKNOWN", login: a?.login || "UNKNOWN", mutation_uncertain: a?.journal?.status === "DISPATCHED", error: this.lastError, live: "OFF"};
  }
  event(type, payload, commandId = this.active?.command_id, identity = this.active) {
    if (!this.connected || !this.port) return false;
    try { this.port.postMessage(envelope(type, identity, payload, commandId, this.uuid, this.now())); return true; }
    catch { this.lastError = "NATIVE_WRITE_FAILED"; void this.disconnected(); return false; }
  }
  ready(commandId = this.connectionId) {
    const a = this.active;
    const payload = {version: EXTENSION_VERSION, stage: a?.state || "IDLE", challenge: a?.challenge || "UNKNOWN", login: a?.login || "UNKNOWN"};
    if (Number.isInteger(a?.tab_id)) payload.tab_id = a.tab_id;
    if (a?.journal?.status === "DISPATCHED") payload.mutation_uncertain = true;
    this.event("EDGE_READY", payload, commandId, null);
    this.lastHeartbeat = new Date(this.now()).toISOString();
  }
  async attach(port) {
    this.port = port; this.connected = true; this.connectionId = this.uuid(); this.lastError = null;
    // Reconnection reports state only. It never schedules a pending mutation.
    this.ready();
  }
  async disconnected() {
    this.connected = false; this.port = null; this.revision += 1; this.normalObserved = false;
    if (this.active && !TERMINAL.has(this.active.state)) this.active.state = "PAUSED_DISCONNECTED";
    await this.save();
  }
  async handle(message) {
    try { validateEnvelope(message, "command", this.now()); }
    catch (error) { this.lastError = error.message; return {accepted: false, code: error.message}; }
    if (!this.connected) return {accepted: false, code: "NATIVE_DISCONNECTED"};
    if (this.seen.some(item => item.message_id === message.message_id || item.command_id === message.command_id)) return {accepted: false, code: "REPLAY_REJECTED"};
    const transport = this.port, entryRevision = this.revision;
    this.seen.push({message_id: message.message_id, command_id: message.command_id}); this.seen = this.seen.slice(-256);
    // Persist command receipt before opening a tab or issuing any DOM operation.
    await this.save();
    if (!this.connected || this.port !== transport || this.revision !== entryRevision) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
    if (["PING", "GET_STATUS"].includes(message.type)) { this.ready(message.command_id); return {accepted: true}; }
    if (message.type === "DISARM") {
      this.revision += 1; this.normalObserved = false;
      if (this.active && !TERMINAL.has(this.active.state)) this.active.state = "DISARMED";
      await this.save(); this.ready(message.command_id); return {accepted: true};
    }
    if (message.type === "START_CHECKOUT") { this.event("FAILED", {code: "LIVE_NOT_ENABLED", stage: "DISARMED"}, message.command_id, message); return {accepted: false, code: "LIVE_NOT_ENABLED"}; }
    if (message.provider !== "bandwagon" && message.payload.product) {
      const url = new URL(message.payload.product.url);
      // Opening an add URL is already a mutation, even without a DOM click.
      if (url.searchParams.get("a") === "add" || url.searchParams.get("action") === "add") return this.reject(message, "ADAPTER_READ_ONLY");
    }
    if (message.type === "CANCEL_INTENT") {
      if (!this.matches(message)) return this.reject(message, "INTENT_MISMATCH");
      this.revision += 1; this.active.state = "CANCELLED"; this.active.command_id = message.command_id;
      await this.save(); this.event("FAILED", {code: "CANCELLED", stage: "CANCELLED"}); return {accepted: true};
    }
    if (message.type === "RESUME_INTENT") {
      if (!this.matches(message)) return this.reject(message, "INTENT_MISMATCH");
      if (!["WAITING_FOR_HUMAN", "PAUSED_DISCONNECTED", "PAUSED_RESTART", "PAUSED_UNCERTAIN", "DISARMED", "FAILED"].includes(this.active.state)) return this.reject(message, "RESUME_NOT_ALLOWED");
      if (!this.normalObserved || this.active.challenge !== "NONE") return this.reject(message, "NORMAL_PAGE_NOT_VERIFIED");
      if (!sameProduct(message.payload.product, this.active.product)) return this.reject(message, "PRODUCT_MISMATCH");
      this.active.cart_resume_check = this.active.checkpoint === "CART_READY";
      this.active.empty_recheck_document = null;
      this.active.command_id = message.command_id; this.active.state = "RUNNING"; this.revision += 1;
      const resumedRevision = this.revision;
      await this.save();
      return {accepted: this.connected && this.port === transport && this.revision === resumedRevision};
    }
    if (this.active && (!TERMINAL.has(this.active.state) || this.matches(message))) {
      // An explicit OPEN_PRODUCT may be followed by DRY_RUN for that same intent.
      if (!(message.type === "START_DRY_RUN" && this.matches(message) && this.active.state === "OPENED" && !this.active.journal)) return this.reject(message, "INTENT_ALREADY_ACTIVE");
      if (!sameProduct(message.payload.product, this.active.product)) return this.reject(message, "PRODUCT_MISMATCH");
      this.active.command_id = message.command_id; this.active.open_only = false; this.active.state = "RUNNING"; this.revision += 1;
      const startedRevision = this.revision;
      await this.save();
      return {accepted: this.connected && this.port === transport && this.revision === startedRevision};
    }
    if (this.active?.journal?.status === "DISPATCHED") return this.reject(message, "UNRESOLVED_MUTATION");
    this.revision += 1; this.normalObserved = false;
    this.active = {intent_id: message.intent_id, provider: message.provider, product_id: message.product_id, command_id: message.command_id, product: message.payload.product, state: "RUNNING", checkpoint: "START", tab_id: null, journal: null, pid_bound: false, bound_cart_id: null, document_id: null, challenge: "UNKNOWN", login: "UNKNOWN", open_only: message.type === "OPEN_PRODUCT"};
    const started = this.active, startedRevision = this.revision;
    await this.save();
    if (!this.connected || this.port !== transport || this.revision !== startedRevision || this.active !== started) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
    let tab;
    try { tab = await this.api.tabs.create({url: message.payload.product.url, active: true}); }
    catch { await this.pause("FAILED", "TAB_OPEN_FAILED", "FAILED"); return {accepted: false}; }
    if (this.active !== started) return {accepted: false, code: "COMMAND_CONTEXT_CHANGED"};
    started.tab_id = tab.id; await this.save();
    return {accepted: this.connected && this.port === transport && this.revision === startedRevision};
  }
  matches(message) { return this.active && message.provider === (this.active.provider || "bandwagon") && message.intent_id === this.active.intent_id && message.product_id === this.active.product_id; }
  reject(message, code) { this.event("FAILED", {code, stage: "PAUSED"}, message.command_id, message); return {accepted: false, code}; }
  expected() { return {...this.active.product, product_id: this.active.product_id}; }
  async read(operation = "detectPage") {
    const active = this.active, tabId = active.tab_id;
    const provider = active.provider || "bandwagon";
    const request = {source: "AUTOGRAB_COMPANION", provider, operation, expected: this.expected()};
    if (active.document_id) {
      try { return await this.api.tabs.sendMessage(tabId, request, {documentId: active.document_id}); }
      catch { /* Only reads may discover a new document after navigation. */ }
    }
    const files = {bandwagon: "providers/bandwagon/adapter.js", dmit: "providers/dmit/adapter.js", vmiss: "providers/vmiss/adapter.js", vps: "providers/vps/adapter.js", apple: "providers/apple/adapter.js"};
    if (!Object.hasOwn(files, provider)) throw new Error("UNKNOWN_PROVIDER");
    const results = await this.api.scripting.executeScript({target: {tabId}, files: [files[provider], "content.js"]});
    const frame = results.find(item => item.frameId === 0 && typeof item.documentId === "string");
    if (!frame || this.active !== active) throw new Error("DOCUMENT_NOT_BOUND");
    active.document_id = frame.documentId;
    return this.api.tabs.sendMessage(tabId, request, {documentId: frame.documentId});
  }
  async focus() {
    try {
      const tab = await this.api.tabs.get(this.active.tab_id);
      await this.api.tabs.update(tab.id, {active: true});
      if (Number.isInteger(tab.windowId)) await this.api.windows.update(tab.windowId, {focused: true});
    } catch { /* A closed tab is reported on the next read. */ }
  }
  async pause(state, code, eventType = "FAILED", result = {}) {
    if (!this.active) return;
    this.active.state = state; this.revision += 1;
    // Every pause starts a new observation generation, including site changes.
    // A normal page observed before this pause cannot authorize its resume.
    this.normalObserved = false;
    if (result.challenge) this.active.challenge = result.challenge;
    if (result.login) this.active.login = result.login;
    await this.save();
    this.event(eventType, {stage: state, code, login: this.active.login, challenge: this.active.challenge, resume_from: this.active.checkpoint, mutation_uncertain: this.active.journal?.status === "DISPATCHED", tab_id: this.active.tab_id});
    if (state === "WAITING_FOR_HUMAN") await this.focus();
  }
  async pageOpened(result, code) {
    const active = this.active, revision = this.revision;
    if (!active) return;
    let url;
    try { url = sanitizeURL((await this.api.tabs.get(active.tab_id)).url, active.provider || "bandwagon"); } catch { return; }
    if (active !== this.active || revision !== this.revision || !this.connected) return;
    const payload = {tab_id: active.tab_id, stage: result.stage || "UNKNOWN", login: result.login || "UNKNOWN", challenge: result.challenge || "UNKNOWN"};
    if (url) payload.url = url;
    if (code) payload.code = code;
    this.event("PAGE_OPENED", payload, active.command_id, active);
  }
  async checkResult(result) {
    if (!result || typeof result !== "object") { await this.pause("FAILED", "INVALID_PAGE_RESULT"); return false; }
    this.active.challenge = result.challenge || "UNKNOWN"; this.active.login = result.login || "UNKNOWN";
    if (result.code === "RATE_LIMITED") { await this.pause("FAILED", "RATE_LIMITED", "SITE_CHANGED", result); return false; }
    if (result.code === "CONFIGURATION_UNCERTAIN") { await this.pause("PAUSED_UNCERTAIN", "CONFIGURATION_UNCERTAIN", "SITE_CHANGED", result); return false; }
    if (result.code === "CONFIGURATION_INVALID") { await this.pause("FAILED", "CONFIGURATION_INVALID", "SITE_CHANGED", result); return false; }
    if (result.challenge === "REQUIRED" || result.code === "HUMAN_CHALLENGE_REQUIRED") { await this.pause("WAITING_FOR_HUMAN", "HUMAN_CHALLENGE_REQUIRED", "HUMAN_CHALLENGE_REQUIRED", result); return false; }
    if (result.login === "REQUIRED" || result.code === "LOGIN_REQUIRED") { result = {...result, login: "REQUIRED"}; await this.pause("WAITING_FOR_HUMAN", "LOGIN_REQUIRED", "LOGIN_REQUIRED", result); return false; }
    return true;
  }
  async mutate(name, expectedRevision = this.revision) {
    const a = this.active, spec = MUTATIONS[name];
    if ((a?.provider || "bandwagon") !== "bandwagon") { await this.pause("PAUSED_UNCERTAIN", "ADAPTER_READ_ONLY", "SITE_CHANGED"); return; }
    if (expectedRevision !== this.revision || !this.connected || a.state !== "RUNNING" || a.journal?.status === "DISPATCHED") return;
    const rev = this.revision;
    if (!a.document_id) { await this.pause("PAUSED_UNCERTAIN", "DOCUMENT_NOT_BOUND", "SITE_CHANGED"); return; }
    a.journal = {name, status: "DISPATCHED", document_id: a.document_id, ticket: this.uuid(), dispatched_at: new Date(this.now()).toISOString()};
    // This write must finish successfully before sending any DOM mutation.
    await this.save();
    if (!this.connected || a.state !== "RUNNING" || rev !== this.revision) return;
    try {
      const result = await this.api.tabs.sendMessage(a.tab_id, {source: "AUTOGRAB_COMPANION", operation: spec.operation, expected: this.expected(), ticket: a.journal.ticket, cart_id: a.bound_cart_id}, {documentId: a.document_id});
      if (rev !== this.revision || !this.connected) return;
      if (result?.ok === false && result.action === "NONE") {
        // The content adapter explicitly proved that no page mutation occurred.
        // Only this response can release an attempted ticket; silence never can.
        a.journal.status = "NOT_APPLIED"; await this.save();
      }
      if (!(await this.checkResult(result))) return;
      if (result.ok === false) await this.pause("PAUSED_UNCERTAIN", result.code || "MUTATION_REJECTED", result.code === "SOLD_OUT" ? "SOLD_OUT" : "SITE_CHANGED", result);
    } catch {
      // Navigation can destroy the response channel; observe the next document.
      // DISPATCHED is retained. Calling this action again is forbidden.
    }
  }
  async verifyMutation(page, expectedRevision = this.revision) {
    const a = this.active, journal = a.journal, spec = MUTATIONS[journal.name];
    if (expectedRevision !== this.revision || a.state !== "RUNNING" || !this.connected) return false;
    if (!spec || page.stage !== spec.target) {
      await this.pause("PAUSED_UNCERTAIN", "MUTATION_OUTCOME_UNCERTAIN", "SITE_CHANGED", page); return false;
    }
    const result = await this.read(spec.verify);
    if (expectedRevision !== this.revision || a !== this.active || a.state !== "RUNNING" || !this.connected) return false;
    if (!(await this.checkResult(result))) return false;
    if (!result.ok) { await this.pause("PAUSED_UNCERTAIN", result.code || "MUTATION_RESULT_UNVERIFIED", "SITE_CHANGED", result); return false; }
    if (journal.name === "OPEN_CONFIGURATION") {
      if (!result.cart_id) { await this.pause("PAUSED_UNCERTAIN", "CART_ID_UNVERIFIED", "SITE_CHANGED", result); return false; }
      a.bound_cart_id = result.cart_id;
    } else if (!["OPEN_CHECKOUT", "SELECT_PUBLIC_BILLING"].includes(journal.name) && (!a.bound_cart_id || result.cart_id !== a.bound_cart_id)) {
      await this.pause("PAUSED_UNCERTAIN", "CART_ID_CHANGED", "SITE_CHANGED", result); return false;
    }
    journal.status = "VERIFIED"; journal.verified_at = new Date(this.now()).toISOString();
    a.checkpoint = spec.checkpoint;
    if (journal.name === "OPEN_CONFIGURATION") a.pid_bound = true;
    await this.save();
    if (spec.checkpoint === "CART_READY") {
      if (!result.cart_id) { await this.pause("PAUSED_UNCERTAIN", "CART_ID_UNVERIFIED", "SITE_CHANGED", result); return false; }
      this.event("CART_READY", {stage: "CART_READY", cart_id: result.cart_id, login: result.login, challenge: result.challenge, tab_id: a.tab_id});
    }
    if (spec.checkpoint === "CHECKOUT_READY") {
      if (result.login !== "VALID" || result.challenge !== "NONE") { await this.pause("WAITING_FOR_HUMAN", "CHECKOUT_SESSION_UNVERIFIED", "LOGIN_REQUIRED", result); return false; }
      a.state = "CHECKOUT_READY"; await this.save();
      this.event("CHECKOUT_READY", {stage: "CHECKOUT_READY", login: "VALID", challenge: "NONE", tab_id: a.tab_id});
    }
    return true;
  }
  archiveCartJournal(resolution) {
    const a = this.active;
    if (a.journal) {
      a.journal_history ??= [];
      a.journal_history.push({...a.journal, resolution, resolved_at: new Date(this.now()).toISOString()});
      a.journal = null;
    }
  }
  async resumeCart(page, revision) {
    const a = this.active;
    const current = () => a === this.active && revision === this.revision && this.connected && a.state === "RUNNING";
    if (!a.pid_bound || !a.bound_cart_id || !["CART", "CHECKOUT"].includes(page.stage)) {
      await this.pause("PAUSED_UNCERTAIN", "CART_REVIEW_REQUIRED", "SITE_CHANGED", page); return;
    }
    const checked = await this.read(page.stage === "CHECKOUT" ? "verifyCheckout" : "verifyCart");
    if (!current() || !(await this.checkResult(checked))) return;
    if (checked.ok && (page.stage === "CHECKOUT" ? checked.login === "VALID" : checked.cart_id === a.bound_cart_id)) {
      this.archiveCartJournal(page.stage === "CHECKOUT" ? "CHECKOUT_VERIFIED" : "CART_PRESENT_VERIFIED");
      a.cart_resume_check = false; a.empty_recheck_document = null;
      if (page.stage === "CHECKOUT") { a.checkpoint = "CHECKOUT_READY"; a.state = "CHECKOUT_READY"; }
      await this.save();
      if (a !== this.active || revision !== this.revision || !this.connected) return;
      this.event(page.stage === "CHECKOUT" ? "CHECKOUT_READY" : "CART_READY", {stage: a.checkpoint, cart_id: a.bound_cart_id, login: checked.login, challenge: checked.challenge, tab_id: a.tab_id});
      return;
    }
    if (page.stage !== "CART" || checked.code !== "CART_EMPTY" || checked.challenge !== "NONE") {
      await this.pause("PAUSED_UNCERTAIN", checked.code || "CART_REVIEW_REQUIRED", "SITE_CHANGED", checked); return;
    }
    // An explicit empty basket must survive a new, read-only Cart document.
    // The source configuration page or a missing row is never empty evidence.
    if (!a.empty_recheck_document) {
      a.empty_recheck_document = a.document_id;
      await this.save();
      if (!current()) return;
      await this.pageOpened(checked, "CART_STATE_STALE");
      if (current()) await this.api.tabs.update(a.tab_id, {url: "https://bandwagonhost.com/cart.php?a=view"});
      return;
    }
    if (a.document_id === a.empty_recheck_document) {
      await this.pause("PAUSED_UNCERTAIN", "CART_EMPTY_RECHECK_REQUIRED", "SITE_CHANGED", checked); return;
    }
    const count = a.cart_rebuild_count ?? 0;
    if (!Number.isInteger(count) || count < 0 || count >= MAX_CART_REBUILDS) {
      await this.pause("PAUSED_UNCERTAIN", "CART_REBUILD_LIMIT", "SITE_CHANGED", checked); return;
    }
    this.archiveCartJournal("NEW_DOCUMENT_CART_EMPTY_CONFIRMED");
    a.cart_rebuild_count = count + 1;
    a.checkpoint = "START"; a.pid_bound = false; a.bound_cart_id = null;
    a.cart_resume_check = false; a.empty_recheck_document = null;
    await this.save();
    if (!current()) return;
    await this.pageOpened(checked, "CART_REBUILDING");
    if (current()) await this.api.tabs.update(a.tab_id, {url: a.product.url});
  }
  async pump() {
    if (this.pumping || !this.active || !this.connected || !Number.isInteger(this.active.tab_id) || TERMINAL.has(this.active.state)) return;
    this.pumping = true;
    const rev = this.revision;
    try {
      const tab = await this.api.tabs.get(this.active.tab_id);
      if (tab.status === "loading") return;
      if (!tab.url || !allowedOrigin(tab.url, this.active.provider || "bandwagon")) {
        if (this.active.state !== "FAILED") await this.pause("FAILED", "UNEXPECTED_ORIGIN", "SITE_CHANGED");
        return;
      }
      const page = await this.read();
      if (rev !== this.revision || !this.connected) return;
      if (this.active.state !== "RUNNING" && this.active.state !== "OPENED") {
        // A challenge page is observed passively; no configure/click runs here.
        if (page.challenge === "NONE" && page.login !== "REQUIRED" && !["RATE_LIMITED", "CONFIGURATION_UNCERTAIN", "CONFIGURATION_INVALID"].includes(page.code) && !["UNKNOWN", "LOGIN", "HUMAN_CHALLENGE"].includes(page.stage)) {
          this.active.challenge = "NONE"; this.active.login = page.login || "UNKNOWN";
          if (!this.normalObserved) { this.normalObserved = true; await this.save(); await this.pageOpened(page); }
        }
        return;
      }
      if (!(await this.checkResult(page))) return;
      if (page.challenge !== "NONE") { await this.pause("FAILED", "PAGE_SAFETY_UNVERIFIED", "SITE_CHANGED", page); return; }
      await this.pageOpened(page);
      if (rev !== this.revision || !this.connected) return;
      if (this.active.open_only) { this.active.state = "OPENED"; await this.save(); return; }
      if ((this.active.provider || "bandwagon") !== "bandwagon") { await this.pause("PAUSED_UNCERTAIN", "ADAPTER_READ_ONLY", "SITE_CHANGED", page); return; }
      if (this.active.cart_resume_check) { await this.resumeCart(page, rev); return; }
      if (this.active.journal?.status === "DISPATCHED") { await this.verifyMutation(page, rev); return; }
      const a = this.active;
      if (a.checkpoint === "START") {
        if (page.stage !== "PRODUCT") { await this.pause("FAILED", "PRODUCT_PAGE_UNVERIFIED", "SITE_CHANGED", page); return; }
        await this.mutate("SELECT_PUBLIC_BILLING", rev); return;
      }
      if (a.checkpoint === "PUBLIC_BILLING_SELECTED") {
        if (page.stage !== "PRODUCT") { await this.pause("FAILED", "PRODUCT_PAGE_UNVERIFIED", "SITE_CHANGED", page); return; }
        const verified = await this.read("verifyProduct");
        if (rev !== this.revision || a !== this.active || !this.connected) return;
        if (!(await this.checkResult(verified))) return;
        if (!verified.ok) { await this.pause("FAILED", verified.code || "PUBLIC_PRODUCT_UNVERIFIED", "SITE_CHANGED", verified); return; }
        // Public evidence binds observed PID, annual link and amount. The exact
        // product name and currency are still verified on the configuration page.
        await this.mutate("OPEN_CONFIGURATION", rev); return;
      }
      if (!a.pid_bound) { await this.pause("FAILED", "PRODUCT_NAVIGATION_UNVERIFIED", "SITE_CHANGED", page); return; }
      if (a.checkpoint === "CONFIGURATION_OPENED") {
        if (page.stage !== "CONFIGURATION") { await this.pause("FAILED", "CONFIGURATION_CHANGED", "SITE_CHANGED", page); return; }
        await this.mutate("CONFIGURE_PRODUCT", rev); return;
      }
      if (a.checkpoint === "CONFIGURED") {
        const verified = await this.read("verifyProduct");
        if (rev !== this.revision || a !== this.active || !this.connected) return;
        if (!(await this.checkResult(verified))) return;
        if (!verified.ok || !a.bound_cart_id || verified.cart_id !== a.bound_cart_id) { await this.pause("FAILED", verified.ok ? "CART_ID_CHANGED" : verified.code || "PRODUCT_UNVERIFIED", "SITE_CHANGED", verified); return; }
        this.event("PRODUCT_VERIFIED", {stage: "PRODUCT_VERIFIED", login: verified.login, challenge: verified.challenge, tab_id: a.tab_id});
        await this.mutate("ADD_TO_CART", rev); return;
      }
      if (a.checkpoint === "CART_READY") {
        const verified = await this.read("verifyCart");
        if (rev !== this.revision || a !== this.active || !this.connected) return;
        if (!(await this.checkResult(verified))) return;
        if (!verified.ok || !a.bound_cart_id || verified.cart_id !== a.bound_cart_id) { await this.pause("FAILED", verified.ok ? "CART_ID_CHANGED" : verified.code || "CART_CHANGED", "SITE_CHANGED", verified); return; }
        await this.mutate("OPEN_CHECKOUT", rev); return;
      }
      await this.pause("FAILED", "UNKNOWN_CHECKPOINT", "SITE_CHANGED", page);
    } catch { if (this.connected && this.active && rev === this.revision) await this.pause("PAUSED_UNCERTAIN", "PAGE_READ_FAILED", "FAILED"); }
    finally { this.pumping = false; }
  }
}

if (typeof chrome !== "undefined" && chrome.runtime?.id) {
  const controller = new CompanionController(chrome);
  const restored = controller.restore();
  let connecting = false, retryTimer = null, retryDelay = 2500;
  async function connect() {
    await restored;
    if (controller.connected || connecting) return;
    connecting = true;
    try {
      const port = chrome.runtime.connectNative(HOST);
      port.onMessage.addListener(message => { void controller.handle(message).then(() => controller.pump()).catch(() => { controller.lastError = "COMMAND_FAILED"; }); });
      port.onDisconnect.addListener(() => {
        void chrome.runtime.lastError;
        if (controller.port !== port) return;
        void controller.disconnected();
        connecting = false;
        // Only restore the transport. Paused intents require an explicit resume.
        if (!retryTimer) retryTimer = setTimeout(() => { retryTimer = null; void connect(); }, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 60000);
      });
      await controller.attach(port); retryDelay = 2500;
    } catch { controller.lastError = "NATIVE_HOST_UNAVAILABLE"; }
    finally { connecting = false; }
  }
  chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || sender.tab || sender.url !== chrome.runtime.getURL("popup.html") || !message || Object.keys(message).length !== 1 || !["STATUS", "CONNECT"].includes(message.action)) return false;
    (async () => { await restored; if (message.action === "CONNECT") await connect(); return controller.status(); })().then(respond);
    return true;
  });
  chrome.tabs.onUpdated.addListener((tabId, change) => { if (tabId === controller.active?.tab_id && change.status === "complete") void controller.pump(); });
  chrome.tabs.onRemoved.addListener(tabId => { if (tabId === controller.active?.tab_id && !TERMINAL.has(controller.active.state)) void controller.pause("PAUSED_UNCERTAIN", "TAB_CLOSED"); });
  chrome.runtime.onStartup.addListener(() => { void connect(); });
  chrome.runtime.onInstalled.addListener(() => { void connect(); });
  setInterval(() => { if (controller.connected) controller.ready(); }, 20000);
  setInterval(() => { void controller.pump(); }, 1500);
  void connect();
}
