import test from "node:test";
import assert from "node:assert/strict";
import {randomUUID} from "node:crypto";
import {CompanionController} from "../service-worker.js";
const product = {name: "20G KVM - PROMO", url: "https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9", period: "annually", cents: 4999, currency: "USD"};
function command(type = "START_DRY_RUN", identity = {}) {
  return {version: 1, type, message_id: randomUUID(), command_id: randomUUID(), intent_id: identity.intent_id || randomUUID(), product_id: identity.product_id || "87", provider: "bandwagon", timestamp: new Date().toISOString(), payload: ["CANCEL_INTENT"].includes(type) ? {} : {product, mode: "DRY_RUN"}};
}
function control(type) { return {...command(type), intent_id: null, product_id: null, payload: {}}; }
async function fixture(initial = {}) {
  const saved = structuredClone(initial), events = [], actions = [], writes = [], focuses = [];
  const page = {stage: "PRODUCT", challenge: "NONE", login: "VALID", status: "complete", failAfterAction: false, holdAction: false, cart_id: "configuration_0", document_id: "document-1", empty: false};
  let readHook;
  const result = () => ({ok: page.challenge === "NONE", stage: page.challenge === "REQUIRED" ? "HUMAN_CHALLENGE" : page.stage, code: page.challenge === "REQUIRED" ? "HUMAN_CHALLENGE_REQUIRED" : "VERIFIED", challenge: page.challenge, login: page.login, ...(["CART", "CONFIGURATION"].includes(page.stage) ? {cart_id: page.cart_id} : {})});
  const api = {
    storage: {local: {async get(key) {return {[key]: structuredClone(saved[key])};}, async set(value) { Object.assign(saved, structuredClone(value)); writes.push(structuredClone(value)); }}},
    tabs: {
      async create() {return {id: 10};}, async get() {return {id: 10, windowId: 2, status: page.status, url: page.stage === "PRODUCT" ? product.url : `https://bandwagonhost.com/cart.php?a=${page.stage === "CHECKOUT" ? "checkout" : page.stage === "CART" ? "view" : "confproduct"}`};},
      async update(id, options) {
        focuses.push({id, options});
        if (options.url) {
          assert([product.url, "https://bandwagonhost.com/cart.php?a=view"].includes(options.url));
          page.stage = options.url === product.url ? "PRODUCT" : "CART";
          page.document_id += "-get";
        }
        return {id};
      },
      async sendMessage(id, request, options) {
        if (options?.documentId !== page.document_id) throw new Error("old document is gone");
        if (request.ticket) {
          assert.equal(saved.autograbCompanionV1.active.journal.status, "DISPATCHED", "durable marker exists before action");
          assert.equal(saved.autograbCompanionV1.active.journal.ticket, request.ticket);
          actions.push(request.operation);
          if (!page.holdAction) {
            if (["addToCart", "openCheckout"].includes(request.operation) || request.operation === "configureProduct" && page.stage === "PRODUCT") page.document_id += "-next";
            if (request.operation === "configureProduct") page.stage = "CONFIGURATION";
            if (request.operation === "addToCart") { page.stage = "CART"; page.empty = false; }
            if (request.operation === "openCheckout") page.stage = "CHECKOUT";
          }
          if (page.failAfterAction) throw new Error("navigation closed response");
          return {...result(), action: "NAVIGATED"};
        }
        if (readHook) await readHook(request);
        if (request.operation === "verifyCart" && page.stage === "CART" && page.empty && page.challenge === "NONE") return {ok:false, stage:"CART", code:"CART_EMPTY", challenge:"NONE", login:page.login};
        return result();
      },
    }, windows: {async update(id, options) { focuses.push({id, options}); }},
    scripting: {async executeScript() {return [{frameId: 0, documentId: page.document_id}];}},
  };
  const controller = new CompanionController(api, {uuid: randomUUID});
  await controller.restore(); await controller.attach({postMessage: e => events.push(e)});
  return {controller, api, page, saved, events, actions, writes, focuses, setReadHook: f => readHook = f};
}
async function pumps(controller, count = 12) {for (let i=0;i<count;i++) await controller.pump();}
test("observed provider blockers retain their diagnostic and never dispatch", async () => {
  for (const [provider, code, stage, state] of [["vmiss", "RATE_LIMITED", "UNKNOWN", "FAILED"], ["dmit", "CONFIGURATION_UNCERTAIN", "CONFIGURATION", "PAUSED_UNCERTAIN"], ["dmit", "CONFIGURATION_INVALID", "CONFIGURATION", "FAILED"]]) {
    const f = await fixture();
    const start = command("OPEN_PRODUCT");
    start.provider = provider;
    start.product_id = provider === "vmiss" ? "us-los-angeles-cmin2/basic" : "265";
    start.payload = {...start.payload, product: {...start.payload.product, url: provider === "vmiss" ? "https://app.vmiss.com/store/us-los-angeles-cmin2" : "https://www.dmit.io/cart.php"}};
    f.api.tabs.get = async () => ({id: 10, status: "complete", url: start.payload.product.url});
    f.api.tabs.sendMessage = async () => ({ok: false, stage, code, challenge: provider === "dmit" ? "NONE" : "UNKNOWN", login: "UNKNOWN"});
    await f.controller.handle(start); await pumps(f.controller);
    assert.equal(f.controller.active.state, state);
    assert.equal(f.events.filter(e => e.payload.code === code).length, 1);
    assert.equal(f.controller.normalObserved, false);
    assert.deepEqual(f.actions, []);
    assert.equal(f.controller.active.intent_id, start.intent_id);
  }
});
test("real controller performs one dry path and stops before order", async () => {
  const f = await fixture(); const start = command(); await f.controller.handle(start); await pumps(f.controller);
  assert.equal(f.controller.active.state, "CHECKOUT_READY");
  assert.deepEqual(f.actions, ["selectBillingPeriod", "configureProduct", "configureProduct", "addToCart", "openCheckout"]);
  assert.equal(f.events.filter(e => e.type === "CART_READY").length, 1); assert.equal(f.events.filter(e => e.type === "CHECKOUT_READY").length, 1);
  assert(f.events.findIndex(e => e.type === "PAGE_OPENED") < f.events.findIndex(e => e.type === "PRODUCT_VERIFIED"));
  assert(f.events.filter(e => e.intent_id).every(e => e.intent_id === start.intent_id));
  assert.equal(f.events.some(e => ["ORDER_CREATED", "PAYMENT_READY"].includes(e.type)), false);
});
test("START_CHECKOUT fails closed even with valid protocol", async () => {
  const f = await fixture(); const r = await f.controller.handle(command("START_CHECKOUT"));
  assert.equal(r.code, "LIVE_NOT_ENABLED"); assert.equal(f.actions.length, 0); assert.equal(f.controller.active, null);
});
test("provider identity cannot cancel another provider and new adapter never dispatches Bandwagon mutations", async () => {
  const f = await fixture(), start = command();
  await f.controller.handle(start);
  const wrong = {...command("CANCEL_INTENT", start), provider:"dmit"};
  assert.equal((await f.controller.handle(wrong)).code,"INTENT_MISMATCH");
  await f.controller.handle(command("CANCEL_INTENT",start));
  const next = {...command(), provider:"dmit", payload:{mode:"DRY_RUN",product:{...product,url:"https://www.dmit.io/cart.php?gid=1"}}};
  f.api.tabs.get = async()=>({id:10,status:"complete",url:next.payload.product.url});
  await f.controller.handle(next); await pumps(f.controller);
  assert.equal(f.controller.active.provider,"dmit");
  assert.equal(f.controller.active.state,"PAUSED_UNCERTAIN");
  assert.equal(f.events.find(e=>e.payload.code==="ADAPTER_READ_ONLY")?.provider,"dmit");
  assert.deepEqual(f.actions,[]);
  const readonly = await fixture();
  const writeURL = {...next, message_id:randomUUID(), command_id:randomUUID(), payload:{...next.payload,product:{...next.payload.product,url:"https://www.dmit.io/cart.php?a=add&pid=87"}}};
  assert.equal((await readonly.controller.handle(writeURL)).code,"ADAPTER_READ_ONLY");
  assert.equal(readonly.controller.active,null);
});
test("replay and wrong intent cannot mutate active intent", async () => {
  const f = await fixture(); const start = command(); await f.controller.handle(start);
  assert.equal((await f.controller.handle(start)).code, "REPLAY_REJECTED");
  assert.equal((await f.controller.handle({...start, message_id: randomUUID()})).code, "REPLAY_REJECTED");
  assert.equal((await f.controller.handle(command("CANCEL_INTENT"))).code, "INTENT_MISMATCH");
  assert.equal(f.controller.active.intent_id, start.intent_id); assert.equal(f.actions.length, 0);
});
test("disconnect and reconnect never replay mutations", async () => {
  const f = await fixture(); const start = command(); await f.controller.handle(start); await f.controller.pump();
  assert.equal(f.actions.length, 1); await f.controller.disconnected();
  await f.controller.attach({postMessage: e => f.events.push(e)}); await pumps(f.controller);
  assert.equal(f.actions.length, 1); assert.equal(f.controller.active.state, "PAUSED_DISCONNECTED");
  assert.equal((await f.controller.handle(command("RESUME_INTENT", start))).accepted, true);
  await pumps(f.controller); assert.equal(f.controller.active.state, "CHECKOUT_READY"); assert.equal(f.actions.filter(a => a === "addToCart").length, 1);
});
test("human challenge pauses and explicit resume keeps original intent", async () => {
  const f = await fixture(); const start = command(); f.page.challenge = "REQUIRED";
  await f.controller.handle(start); await f.controller.pump();
  assert.equal(f.controller.active.state, "WAITING_FOR_HUMAN"); assert.equal(f.actions.length, 0); assert(f.focuses.length > 0);
  assert.equal((await f.controller.handle(command("RESUME_INTENT", start))).code, "NORMAL_PAGE_NOT_VERIFIED");
  f.page.challenge = "NONE"; await pumps(f.controller, 2); assert.equal(f.actions.length, 0);
  await f.controller.handle(command("RESUME_INTENT", start)); await pumps(f.controller);
  assert.equal(f.controller.active.intent_id, start.intent_id); assert.equal(f.controller.active.state, "CHECKOUT_READY");
});
test("challenge after dispatched cart observes result without repeating cart", async () => {
  const f = await fixture(); const start = command(); await f.controller.handle(start); await pumps(f.controller, 7);
  assert.equal(f.actions.filter(a => a === "addToCart").length, 1);
  f.page.challenge = "REQUIRED"; await f.controller.pump(); assert.equal(f.controller.active.state, "WAITING_FOR_HUMAN");
  f.page.challenge = "NONE"; await f.controller.pump(); await f.controller.handle(command("RESUME_INTENT", start)); await pumps(f.controller);
  assert.equal(f.actions.filter(a => a === "addToCart").length, 1); assert.equal(f.controller.active.state, "CHECKOUT_READY");
});
test("lost mutation response is reconciled from resulting page", async () => {
  const f = await fixture(); f.page.failAfterAction = true; await f.controller.handle(command()); await pumps(f.controller);
  assert.equal(f.controller.active.state, "CHECKOUT_READY"); assert.equal(f.actions.filter(a => a === "addToCart").length, 1);
});
test("uncertain mutation on unchanged source page never repeats", async () => {
  const f = await fixture(); const start = command(); await f.controller.handle(start); await pumps(f.controller, 6);
  f.page.holdAction = true; await f.controller.pump(); await f.controller.pump();
  assert.equal(f.controller.active.state, "PAUSED_UNCERTAIN"); assert.equal(f.controller.active.journal.status, "DISPATCHED");
  await f.controller.pump(); await f.controller.handle(command("RESUME_INTENT", start)); await pumps(f.controller);
  assert.equal(f.actions.filter(a => a === "addToCart").length, 1); assert.equal(f.controller.active.state, "PAUSED_UNCERTAIN");
});
test("worker restart restores pending marker and requires explicit resume", async () => {
  const f = await fixture(); await f.controller.handle(command()); await f.controller.pump();
  const restored = await fixture(f.saved); restored.page.stage = "CONFIGURATION"; await pumps(restored.controller);
  assert.equal(restored.controller.active.state, "PAUSED_RESTART"); assert.equal(restored.actions.length, 0); assert.equal(restored.controller.active.journal.status, "DISPATCHED");
});
test("DISARM during a read prevents subsequent mutation", async () => {
  const f = await fixture(); await f.controller.handle(command()); await pumps(f.controller, 6);
  f.setReadHook(async r => { if (r.operation === "verifyProduct") await f.controller.handle(control("DISARM")); });
  await f.controller.pump(); assert.equal(f.controller.active.state, "DISARMED"); assert.equal(f.actions.includes("addToCart"), false);
});
test("DISARM during mutation result verification cannot advance checkout", async () => {
  const f = await fixture(); await f.controller.handle(command()); await pumps(f.controller, 9);
  f.setReadHook(async r => {if (r.operation === "verifyCheckout") await f.controller.handle(control("DISARM"));});
  await f.controller.pump(); assert.equal(f.controller.active.state, "DISARMED"); assert.equal(f.events.some(e => e.type === "CHECKOUT_READY"), false);
});
test("durability failure before dispatch prevents action", async () => {
  const f = await fixture(); await f.controller.handle(command());
  f.api.storage.local.set = async () => {throw new Error("disk unavailable");};
  await assert.rejects(f.controller.pump()); assert.equal(f.actions.length, 0);
});
test("wrong resume product is rejected without replacing persisted expectation", async () => {
  const f = await fixture(); const start = command(); await f.controller.handle(start); await f.controller.disconnected(); await f.controller.attach({postMessage: e => f.events.push(e)}); await f.controller.pump();
  const resume = command("RESUME_INTENT", start); resume.payload = {...resume.payload, product: {...product, cents: 4000}};
  assert.equal((await f.controller.handle(resume)).code, "PRODUCT_MISMATCH"); assert.equal(f.controller.active.product.cents, 4999);
});
test("OPEN_PRODUCT only opens and reports; same intent may explicitly start dry run", async () => {
  const f = await fixture(); const start = command("OPEN_PRODUCT"); await f.controller.handle(start); await pumps(f.controller, 3);
  assert.equal(f.controller.active.state, "OPENED"); assert.equal(f.actions.length, 0);
  await f.controller.handle(command("START_DRY_RUN", start)); await pumps(f.controller); assert.equal(f.controller.active.state, "CHECKOUT_READY");
});
test("disconnect while persisting received command cannot create RUNNING intent", async () => {
  const f = await fixture(), original = f.api.storage.local.set;
  let first = true;
  f.api.storage.local.set = async value => {await original(value); if (first) {first=false; await f.controller.disconnected();}};
  const result=await f.controller.handle(command());
  assert.equal(result.accepted,false); assert.equal(f.controller.active,null);
  await f.controller.attach({postMessage:e=>f.events.push(e)});await pumps(f.controller);assert.equal(f.actions.length,0);
});
test("disconnect while persisting new intent preserves pause on reconnect", async () => {
  const f=await fixture(),original=f.api.storage.local.set;let count=0;
  f.api.storage.local.set=async value=>{await original(value);count++;if(count===2)await f.controller.disconnected();};
  assert.equal((await f.controller.handle(command())).accepted,false);
  assert.equal(f.controller.active.state,"PAUSED_DISCONNECTED");assert.equal(f.controller.active.tab_id,null);
  await f.controller.attach({postMessage:e=>f.events.push(e)});await pumps(f.controller);assert.equal(f.actions.length,0);
});
test("disconnect while opening a tab never rearms after reconnect", async () => {
  const f=await fixture();f.api.tabs.create=async()=>{await f.controller.disconnected();return{id:10};};
  assert.equal((await f.controller.handle(command())).accepted,false);assert.equal(f.controller.active.state,"PAUSED_DISCONNECTED");
  await f.controller.attach({postMessage:e=>f.events.push(e)});await pumps(f.controller);assert.equal(f.actions.length,0);
});
test("explicit NONE on a pre-action challenge releases only that unused attempt",async()=>{
  const f=await fixture(),start=command(),original=f.api.tabs.sendMessage;let first=true;
  f.api.tabs.sendMessage=async(id,r,options)=>{if(r.ticket&&first){first=false;f.page.challenge="REQUIRED";return{ok:false,stage:"HUMAN_CHALLENGE",code:"HUMAN_CHALLENGE_REQUIRED",challenge:"REQUIRED",login:"UNKNOWN",action:"NONE"};}return original(id,r,options);};
  await f.controller.handle(start);await f.controller.pump();assert.equal(f.controller.active.journal.status,"NOT_APPLIED");assert.equal(f.actions.length,0);
  f.page.challenge="NONE";await f.controller.pump();await f.controller.handle(command("RESUME_INTENT",start));await pumps(f.controller);
  assert.equal(f.controller.active.state,"CHECKOUT_READY");assert.equal(f.actions.filter(a=>a==="addToCart").length,1);
});
test("resume compares product fields independently of object key order",async()=>{
  const f=await fixture(),start=command();await f.controller.handle(start);await f.controller.disconnected();await f.controller.attach({postMessage:e=>f.events.push(e)});await f.controller.pump();
  const resume=command("RESUME_INTENT",start);resume.payload.product={currency:product.currency,cents:product.cents,period:product.period,url:product.url,name:product.name};
  assert.equal((await f.controller.handle(resume)).accepted,true);
});
test("different configuration index cannot replace initial bound draft",async()=>{
  const f=await fixture();await f.controller.handle(command());await pumps(f.controller,6);
  assert.equal(f.controller.active.bound_cart_id,"configuration_0");f.page.cart_id="configuration_1";
  await pumps(f.controller,3);assert.equal(f.actions.includes("addToCart"),false);assert.equal(f.controller.active.state,"FAILED");
});
test("cart result must retain original configuration identity",async()=>{
  const f=await fixture();await f.controller.handle(command());await pumps(f.controller,7);f.page.cart_id="configuration_1";
  await pumps(f.controller,3);assert.equal(f.actions.includes("openCheckout"),false);assert.equal(f.controller.active.state,"PAUSED_UNCERTAIN");
});
test("document navigation between read and write cannot dispatch in replacement document",async()=>{
  const f=await fixture();await f.controller.handle(command());await pumps(f.controller,6);
  const original=f.api.storage.local.set;f.api.storage.local.set=async value=>{await original(value);if(value.autograbCompanionV1.active.journal?.name==="ADD_TO_CART")f.page.document_id="replacement-document";};
  await pumps(f.controller,3);assert.equal(f.actions.includes("addToCart"),false);assert.equal(f.controller.active.state,"PAUSED_UNCERTAIN");
});
test("public billing selection is journaled and read back before configuration navigation",async()=>{
  const f=await fixture();await f.controller.handle(command());await f.controller.pump();
  assert.deepEqual(f.actions,["selectBillingPeriod"]);assert.equal(f.controller.active.journal.name,"SELECT_PUBLIC_BILLING");assert.equal(f.controller.active.journal.status,"DISPATCHED");
  await f.controller.pump();assert.equal(f.controller.active.checkpoint,"PUBLIC_BILLING_SELECTED");assert.equal(f.events.some(e=>e.type==="PRODUCT_VERIFIED"),false);
  await f.controller.pump();assert.deepEqual(f.actions,["selectBillingPeriod","configureProduct"]);
});
test("legacy unused configuration attempt resumes same intent through public billing",async()=>{
  const f=await fixture(),start=command();await f.controller.handle(start);
  f.controller.active.state="WAITING_FOR_HUMAN";f.controller.active.journal={name:"OPEN_CONFIGURATION",status:"NOT_APPLIED",ticket:randomUUID()};await f.controller.save();
  const recovered=await fixture(f.saved);await recovered.controller.pump();await recovered.controller.handle(command("RESUME_INTENT",start));await pumps(recovered.controller);
  assert.equal(recovered.controller.active.intent_id,start.intent_id);assert.equal(recovered.actions[0],"selectBillingPeriod");assert.equal(recovered.controller.active.state,"CHECKOUT_READY");
});
test("legacy unknown configuration dispatch never falls back to billing or repeats navigation",async()=>{
  const f=await fixture(),start=command();await f.controller.handle(start);
  f.controller.active.state="WAITING_FOR_HUMAN";f.controller.active.journal={name:"OPEN_CONFIGURATION",status:"DISPATCHED",ticket:randomUUID()};await f.controller.save();
  const recovered=await fixture(f.saved);await recovered.controller.pump();await recovered.controller.handle(command("RESUME_INTENT",start));await pumps(recovered.controller);
  assert.equal(recovered.actions.length,0);assert.equal(recovered.controller.active.journal.name,"OPEN_CONFIGURATION");assert.equal(recovered.controller.active.state,"PAUSED_UNCERTAIN");
});
test("public billing result mismatch stops before observed product link navigation",async()=>{
  const f=await fixture(),original=f.api.tabs.sendMessage;f.api.tabs.sendMessage=async(id,r,options)=>r.operation==="verifyProduct"&&f.page.stage==="PRODUCT"?{ok:false,stage:"PRODUCT",code:"BILLING_MISMATCH",login:"UNKNOWN",challenge:"NONE"}:original(id,r,options);
  await f.controller.handle(command());await pumps(f.controller);assert.deepEqual(f.actions,["selectBillingPeriod"]);assert.equal(f.controller.active.state,"PAUSED_UNCERTAIN");
});
test("a later site-change pause emits fresh normal-page evidence after an earlier resume",async()=>{
  const f=await fixture(),start=command();f.page.challenge="REQUIRED";await f.controller.handle(start);await f.controller.pump();
  f.page.challenge="NONE";await f.controller.pump();assert.equal(f.controller.normalObserved,true);
  await f.controller.handle(command("RESUME_INTENT",start));await pumps(f.controller,7);
  assert.equal(f.controller.active.journal.name,"ADD_TO_CART");assert.equal(f.controller.active.journal.status,"DISPATCHED");
  const originalTicket=f.controller.active.journal.ticket;f.page.cart_id="configuration_1";await f.controller.pump();
  assert.equal(f.controller.active.state,"PAUSED_UNCERTAIN");assert.equal(f.controller.normalObserved,false);
  const pageEvents=f.events.filter(e=>e.type==="PAGE_OPENED").length;
  f.page.cart_id="configuration_0";await f.controller.pump();
  assert.equal(f.events.filter(e=>e.type==="PAGE_OPENED").length,pageEvents+1);assert.equal(f.controller.normalObserved,true);
  assert.equal(f.controller.active.journal.ticket,originalTicket);assert.equal(f.controller.active.journal.status,"DISPATCHED");
  assert.equal(f.actions.filter(a=>a==="addToCart").length,1);
  await f.controller.handle(command("RESUME_INTENT",start));await pumps(f.controller);
  assert.equal(f.controller.active.state,"CHECKOUT_READY");assert.equal(f.actions.filter(a=>a==="addToCart").length,1);
});
test("DISARM clears prior normal-page evidence before a later explicit resume",async()=>{
  const f=await fixture(),start=command();await f.controller.handle(start);await f.controller.disconnected();await f.controller.attach({postMessage:e=>f.events.push(e)});await f.controller.pump();
  await f.controller.handle(command("RESUME_INTENT",start));assert.equal(f.controller.normalObserved,true);
  await f.controller.handle(control("DISARM"));assert.equal(f.controller.normalObserved,false);
  assert.equal((await f.controller.handle(command("RESUME_INTENT",start))).code,"NORMAL_PAGE_NOT_VERIFIED");
  const pageEvents=f.events.filter(e=>e.type==="PAGE_OPENED").length;await f.controller.pump();
  assert.equal(f.events.filter(e=>e.type==="PAGE_OPENED").length,pageEvents+1);assert.equal(f.controller.normalObserved,true);assert.equal(f.actions.length,0);
});
test("FAILED resumes an existing verified cart without adding it again",async()=>{
  const f=await fixture(),start=command();await f.controller.handle(start);await pumps(f.controller,8);
  assert.equal(f.controller.active.checkpoint,"CART_READY");const previous=structuredClone(f.controller.active.journal);
  const officialGet=f.api.tabs.get;f.api.tabs.get=async()=>({...await officialGet(),url:"https://example.org/"});
  await f.controller.pump();assert.equal(f.controller.active.state,"FAILED");
  const writes=f.writes.length,events=f.events.length;await pumps(f.controller,3);
  assert.equal(f.writes.length,writes);assert.equal(f.events.length,events);
  f.api.tabs.get=officialGet;await f.controller.pump();
  assert.equal((await f.controller.handle(command("RESUME_INTENT",start))).accepted,true);await pumps(f.controller,4);
  assert.equal(f.controller.active.state,"CHECKOUT_READY");assert.equal(f.controller.active.intent_id,start.intent_id);
  assert.equal(f.actions.filter(a=>a==="addToCart").length,1);assert.equal(f.controller.active.cart_rebuild_count,undefined);
  assert(f.controller.active.journal_history.some(j=>j.ticket===previous.ticket&&j.status===previous.status&&j.resolution==="CART_PRESENT_VERIFIED"));
});
test("two confirmed empty documents allow only two same-intent rebuilds across restarts",async()=>{
  let f=await fixture();const start=command();await f.controller.handle(start);await pumps(f.controller,8);
  const oldTickets=[];
  for(let attempt=1;attempt<=2;attempt++){
    oldTickets.push(f.controller.active.journal.ticket);f.page.empty=true;await f.controller.pause("FAILED","CART_STATE_STALE");await f.controller.pump();
    await f.controller.handle(command("RESUME_INTENT",start));const oldDocument=f.page.document_id,actionsBefore=f.actions.length;
    await f.controller.pump();assert.notEqual(f.page.document_id,oldDocument);assert.equal(f.controller.active.cart_rebuild_count??0,attempt-1);assert.equal(f.actions.length,actionsBefore);
    await f.controller.pump();assert.equal(f.controller.active.cart_rebuild_count,attempt);assert.equal(f.controller.active.checkpoint,"START");
    assert.equal(f.page.stage,"PRODUCT");assert.equal(f.events.filter(e=>e.payload.code==="CART_REBUILDING").length,1);
    assert(f.controller.active.journal_history.some(j=>j.ticket===oldTickets.at(-1)&&j.resolution==="NEW_DOCUMENT_CART_EMPTY_CONFIRMED"));
    f=await fixture(f.saved);assert.equal(f.controller.active.cart_rebuild_count,attempt);assert.equal(f.controller.active.intent_id,start.intent_id);
    await f.controller.pump();assert.equal(f.actions.length,0);await f.controller.handle(command("RESUME_INTENT",start));await pumps(f.controller,8);
    assert.equal(f.controller.active.checkpoint,"CART_READY");assert.equal(f.actions.filter(a=>a==="addToCart").length,1);assert(!oldTickets.includes(f.controller.active.journal.ticket));
  }
  f.page.empty=true;await f.controller.pause("FAILED","CART_STATE_STALE");await f.controller.pump();await f.controller.handle(command("RESUME_INTENT",start));
  const previous=structuredClone(f.controller.active.journal),before=f.actions.length;await pumps(f.controller,2);
  assert.equal(f.controller.active.state,"PAUSED_UNCERTAIN");assert.equal(f.controller.active.cart_rebuild_count,2);
  assert.equal(f.events.at(-1).payload.code,"CART_REBUILD_LIMIT");assert.deepEqual(f.controller.active.journal,previous);assert.equal(f.actions.length,before);
});
test("interrupted empty recovery emits no stale evidence and later reuses a nonempty cart",async()=>{
  const f=await fixture(),start=command();await f.controller.handle(start);await pumps(f.controller,8);
  f.page.empty=true;await f.controller.pause("FAILED","CART_STATE_STALE");await f.controller.pump();await f.controller.handle(command("RESUME_INTENT",start));
  const original=f.api.tabs.get,oldTicket=f.controller.active.journal.ticket;let interrupted=false;
  f.api.tabs.get=async(...args)=>{if(f.controller.active.empty_recheck_document&&!interrupted){interrupted=true;await f.controller.handle(control("DISARM"));}return original(...args);};
  await f.controller.pump();assert.equal(f.controller.active.state,"DISARMED");assert.equal(f.controller.active.cart_rebuild_count,undefined);
  assert.equal(f.controller.active.journal.ticket,oldTicket);assert.equal(f.events.some(e=>e.type==="PAGE_OPENED"&&(e.payload.code==="CART_STATE_STALE"||e.payload.code==="CART_REBUILDING")),false);
  assert.equal(f.focuses.some(item=>item.options.url),false);
  f.page.empty=false;await f.controller.pump();await f.controller.handle(command("RESUME_INTENT",start));await pumps(f.controller,4);
  assert.equal(f.controller.active.state,"CHECKOUT_READY");assert.equal(f.actions.filter(a=>a==="addToCart").length,1);
});
