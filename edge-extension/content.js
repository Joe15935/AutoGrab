(() => {
  if (globalThis.__autoGrabCompanionInstalled) return;
  globalThis.__autoGrabCompanionInstalled = true;
  const READS = new Set(["detectPage", "verifyProduct", "verifyCart", "verifyCheckout"]);
  const ACTIONS = new Set(["selectBillingPeriod", "configureProduct", "addToCart", "openCheckout"]);
  const ORDER_OPS = {orderPrecheck: "precheck", submitOrder: "submitOrder", reconcileOrder: "reconcileOrder"};
  const ALLOWED = new Set(["ok", "stage", "code", "login", "challenge", "cart_id", "action", "outcome", "order_id", "invoice_id", "amount_cents", "currency", "official_url", "unpaid", "observed_at", "created_at", "product_verified", "billing", "no_charge_verified", "scope_complete", "time_window_verified", "submission_nonce"]);
  const seenTickets = new Set();
  const providers = {
    bandwagon: {origins: ["https://bandwagonhost.com"], adapter: "AutoGrabBandwagon"},
    dmit: {origins: ["https://www.dmit.io"], adapter: "AutoGrabDMIT"},
    vmiss: {origins: ["https://app.vmiss.com"], adapter: "AutoGrabVMISS"},
    vps: {origins: ["https://v.ps", "https://vps.hosting"], adapter: "AutoGrabVPS"},
    apple: {origins: ["https://www.apple.com", "https://www.apple.com.cn"], adapter: "AutoGrabApple"},
  };
  let actionRunning = false;
  function safeResult(result) {
    if (!result || typeof result !== "object") throw new Error("INVALID_ADAPTER_RESULT");
    const output = {};
    for (const [key, value] of Object.entries(result)) {
      if (ALLOWED.has(key) && (["string", "boolean"].includes(typeof value) || key === "amount_cents" && Number.isSafeInteger(value) && value > 0 && value < 1e12)) output[key] = value;
    }
    return output;
  }
  function validExpected(value, provider) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    if (Object.keys(value).sort().join(",") !== "cents,currency,name,period,product_id,url") return false;
    let u; try { u = new URL(value.url); } catch { return false; }
    if (provider !== "bandwagon") {
      const id = provider === "apple" ? /^[a-z]{2}:[A-Z0-9]{1,30}\/A$/ : provider === "vmiss" ? /^[a-z0-9][a-z0-9-]{0,79}\/[a-z0-9][a-z0-9-]{0,79}$/ : /^[1-9][0-9]{0,39}$/;
      return providers[provider]?.origins.includes(u.origin) && !u.username && !u.password && !u.hash &&
        typeof value.name === "string" && value.name.length > 0 && value.name.length <= 200 && !/[\u0000-\u001f\u007f]/.test(value.name) && id.test(value.product_id) &&
        ["monthly", "quarterly", "semiannually", "annually", "biennially", "triennially", "one_time", "unknown"].includes(value.period) &&
        (value.cents === null || Number.isSafeInteger(value.cents) && value.cents > 0 && value.cents < 1000000000000) && ["USD", "EUR", "CNY", "HKD", "CAD", "GBP", null].includes(value.currency);
    }
    return typeof value.name === "string" && value.name.length > 0 && value.name.length <= 200 && u.origin === "https://bandwagonhost.com" && /^\/order\/ecommerce(?:\/[^/?#]+\/[^/?#]+)?\/?$/.test(u.pathname) && !u.search && !u.hash && !u.username && !u.password && ["annually", "biennially"].includes(value.period) && Number.isSafeInteger(value.cents) && value.cents > 0 && value.cents < 1000000000000 && value.currency === "USD" && /^[1-9][0-9]{0,39}$/.test(value.product_id);
  }
  chrome.runtime.onMessage.addListener((request, sender, respond) => {
    const provider = request?.provider || "bandwagon", config = providers[provider];
    if (sender.id !== chrome.runtime.id || sender.tab || !config?.origins.includes(location.origin)) return false;
    if (!request || request.source !== "AUTOGRAB_COMPANION" || !Object.keys(request).every(k => ["source", "provider", "operation", "expected", "ticket", "cart_id", "order_context"].includes(k)) || !(READS.has(request.operation) || ACTIONS.has(request.operation) || Object.hasOwn(ORDER_OPS, request.operation)) || !validExpected(request.expected, provider)) return false;
    (async () => {
      const adapter = globalThis[config.adapter];
      if (!adapter) throw new Error("ADAPTER_UNAVAILABLE");
      const challenge = await adapter.detectChallenge();
      if (challenge === true || challenge?.challenge === "REQUIRED" || challenge?.stage === "HUMAN_CHALLENGE") return safeResult({ok: false, stage: "HUMAN_CHALLENGE", code: "HUMAN_CHALLENGE_REQUIRED", login: "UNKNOWN", challenge: "REQUIRED", action: "NONE"});
      if (Object.hasOwn(ORDER_OPS, request.operation)) {
        const context = request.order_context, keys = request.operation === "submitOrder" ? ["provider", "permit"] : request.operation === "reconcileOrder" ? ["provider", "submission_nonce", "submitted_at", "order_id", "invoice_id"] : ["provider"];
        if (!["bandwagon", "dmit"].includes(provider) || !globalThis.AutoGrabOrders || !context || context.provider !== provider || Object.keys(context).sort().join(",") !== keys.sort().join(",")) throw new Error("ORDER_CONTEXT_INVALID");
        if (request.operation === "submitOrder") {
          if (typeof request.ticket !== "string" || request.ticket !== context.permit?.nonce || seenTickets.has(request.ticket) || actionRunning) return {ok: false, stage: "ORDER_UNCERTAIN", code: "MUTATION_TICKET_REJECTED", outcome: "UNKNOWN", login: "UNKNOWN", challenge: "NONE"};
          seenTickets.add(request.ticket); actionRunning = true;
          try { return safeResult(await globalThis.AutoGrabOrders.submitOrder(request.expected, context)); }
          finally { actionRunning = false; }
        }
        return safeResult(await globalThis.AutoGrabOrders[ORDER_OPS[request.operation]](request.expected, context));
      }
      if (ACTIONS.has(request.operation)) {
        if (provider !== "bandwagon") return {ok: false, stage: "UNKNOWN", code: "ADAPTER_READ_ONLY", action: "NONE", login: "UNKNOWN", challenge: "NONE"};
        if (typeof request.ticket !== "string" || !/^[0-9a-f-]{36}$/i.test(request.ticket) || seenTickets.has(request.ticket) || actionRunning) return {ok: false, stage: "UNKNOWN", code: "MUTATION_TICKET_REJECTED", login: "UNKNOWN", challenge: "NONE"};
        if (request.cart_id !== null && request.cart_id !== undefined) {
          if (typeof request.cart_id !== "string" || !/^configuration_[0-9]+$/.test(request.cart_id)) return {ok: false, stage: "UNKNOWN", code: "CART_ID_CHANGED", action: "NONE", login: "UNKNOWN", challenge: "NONE"};
          const verified = await adapter[request.operation === "openCheckout" ? "verifyCart" : "verifyProduct"](request.expected);
          if (!verified.ok) return safeResult({...verified, action: "NONE"});
          if (verified.cart_id !== request.cart_id) return safeResult({...verified, ok: false, code: "CART_ID_CHANGED", action: "NONE"});
        } else if (!["selectBillingPeriod", "configureProduct"].includes(request.operation)) return {ok: false, stage: "UNKNOWN", code: "CART_ID_UNVERIFIED", action: "NONE", login: "UNKNOWN", challenge: "NONE"};
        // Never release a consumed ticket, including exceptions and navigation.
        seenTickets.add(request.ticket); actionRunning = true;
        try { return safeResult(await adapter[request.operation](request.expected)); }
        finally { actionRunning = false; }
      }
      return safeResult(await adapter[request.operation](request.expected));
    })().then(respond, () => respond({ok: false, stage: "UNKNOWN", code: "ADAPTER_FAILED", login: "UNKNOWN", challenge: "UNKNOWN"}));
    return true;
  });
})();
