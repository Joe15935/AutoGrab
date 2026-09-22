import test from "node:test";
import assert from "node:assert/strict";
import {randomUUID} from "node:crypto";
import {CompanionController} from "../service-worker.js";
import {validateEnvelope} from "../protocol.js";

const product = {name: "Experimental VPS", url: "https://bandwagonhost.com/order/ecommerce", period: "annually", cents: 16999, currency: "USD"};
function command(type, identity, payload = {}) {
  return {version: 1, type, message_id: randomUUID(), command_id: randomUUID(), intent_id: identity.intent_id,
    provider: "bandwagon", product_id: "87", timestamp: new Date().toISOString(),
    payload: {product, mode: type === "SUBMIT_ORDER" ? "REAL_ORDER_SMOKE_TEST" : "DRY_RUN", tab_id: 10, ...payload}};
}
async function fixture(saved = {}) {
  const identity = {intent_id: randomUUID()}, events = [], actions = [], navigations = [];
  const page = {document: "doc1", url: "https://bandwagonhost.com/cart.php?a=checkout", safe: true, fail: false, receipt: null};
  let onSave, c;
  const ready = () => ({ok: page.safe, stage: "ORDER_PRECHECK", code: page.safe ? "ORDER_PRECHECK_VERIFIED" : "NO_CHARGE_UNVERIFIED", outcome: page.safe ? "PRECHECK_READY" : "UNKNOWN", login: "VALID", challenge: "NONE", product_verified: true, no_charge_verified: page.safe, amount_cents: product.cents, currency: "USD", billing: "annually", observed_at: new Date().toISOString()});
  const api = {storage: {local: {
    async get(key) { return {[key]: structuredClone(saved[key])}; },
    async set(value) { Object.assign(saved,structuredClone(value)); if (onSave) await onSave(value); },
  }}, tabs: {
    async get() { return {id:10,url:page.url,status:"complete"}; },
    async update(id, value) { navigations.push(value.url); page.url=value.url; page.document += "next"; return {id}; },
    async sendMessage(id, request, options) {
      assert.equal(options.documentId,page.document);
      if (request.operation === "submitOrder") {
        assert.equal(saved.autograbCompanionV1.active.order_journal.status,"DISPATCHED");
        assert(saved.autograbCompanionV1.orderNonces.includes(request.ticket));
        actions.push(request.operation);
        if (page.fail) throw new Error("lost response after dispatch");
        return {ok:false,stage:"ORDER_UNCERTAIN",code:"ORDER_DISPATCHED",outcome:"UNKNOWN",login:"VALID",challenge:"NONE",action:"SUBMITTED"};
      }
      return request.operation === "reconcileOrder" ? page.receipt || {ok:false,stage:"RECONCILING",code:"RECONCILIATION_SCOPE_UNVERIFIED",outcome:"UNKNOWN",login:"VALID",challenge:"NONE"} : ready();
    },
  }, scripting: {async executeScript() { return [{frameId:0,documentId:page.document}]; }}, windows:{async update(){}}};
  c = new CompanionController(api,{uuid:randomUUID}); await c.restore(); await c.attach({postMessage: e => events.push(e)});
  return {c,api,identity,events,actions,navigations,page,saved,hook: callback => {onSave=callback;}};
}
async function precheck(f) {
  assert.equal((await f.c.handle(command("ORDER_PRECHECK",f.identity))).accepted,true);
  return f.events.findLast(e => e.type === "ORDER_OBSERVATION").payload.precheck_id;
}
function permit(precheckId) { return {nonce:randomUUID(),precheck_id:precheckId,issued_at:new Date().toISOString(),expires_at:new Date(Date.now()+45000).toISOString(),real_order_smoke_test_armed:true}; }

test("default guard, explicit no-charge evidence, fixed schema and stale document reject submission", async () => {
  const f=await fixture();
  const p=permit(randomUUID()), message=command("SUBMIT_ORDER",f.identity,{permit:p});
  for (const bad of [{...p,real_order_smoke_test_armed:false},{...p,expires_at:new Date(Date.now()+120000).toISOString()}]) assert.throws(()=>validateEnvelope({...message,payload:{...message.payload,permit:bad}}));
  assert.throws(()=>validateEnvelope({...message,payload:{...message.payload,mode:"DRY_RUN"}}));
  assert.equal((await f.c.handle(message)).code,"ORDER_PRECHECK_REQUIRED");
  f.page.safe=false; assert.equal(await precheck(f),undefined);
  f.page.safe=true; const id=await precheck(f); f.page.document="doc2";
  assert.equal((await f.c.handle(command("SUBMIT_ORDER",f.identity,{permit:permit(id)}))).code,"ORDER_PRECHECK_CHANGED");
  assert.deepEqual(f.actions,[]);
});

test("nonce persists before one submit; lost response and worker restore only reconcile", async () => {
  const f=await fixture(), id=await precheck(f), p=permit(id); f.page.fail=true;
  await f.c.handle(command("SUBMIT_ORDER",f.identity,{permit:p}));
  assert.deepEqual(f.actions,["submitOrder"]); assert.equal(f.c.active.state,"ORDER_UNCERTAIN");
  const cancel=command("CANCEL_INTENT",f.identity); cancel.payload={};
  await f.c.handle(cancel); assert.equal(f.c.active.state,"ORDER_UNCERTAIN");
  assert.equal((await f.c.handle(command("SUBMIT_ORDER",f.identity,{permit:permit(id)}))).code,"ORDER_PRECHECK_REQUIRED");
  const recovered=await fixture(f.saved); recovered.identity=f.identity;
  await recovered.c.pump(); assert.deepEqual(recovered.actions,[]);
  await recovered.c.handle(command("RECONCILE_ORDER",f.identity,{submission_nonce:p.nonce,submitted_at:p.issued_at,order_id:null,invoice_id:null}));
  assert.equal(recovered.events.findLast(e=>e.type==="ORDER_OBSERVATION").payload.outcome,"UNKNOWN");
  assert.equal(recovered.c.active.order_journal.nonce,p.nonce);
  assert.deepEqual(recovered.actions,[]);
});

test("disarm or storage failure at durable submission boundary prevents any click", async () => {
  for (const stop of ["disarm","disconnect","storage-failure"]) {
    const f=await fixture(), id=await precheck(f); let fired=false;
    f.hook(async value=>{
      if (fired || value.autograbCompanionV1.active?.order_journal?.status!=="DISPATCHED") return;
      fired=true;
      if (stop==="storage-failure") throw new Error("disk unavailable");
      if (stop==="disconnect") return f.c.disconnected();
      const disarm=command("DISARM",f.identity); disarm.intent_id=null; disarm.product_id=null; disarm.payload={};
      await f.c.handle(disarm);
    });
    await f.c.handle(command("SUBMIT_ORDER",f.identity,{permit:permit(id)}));
    assert.deepEqual(f.actions,[],stop); assert(fired);
  }
});

test("only exact known scoped unpaid invoice can become payment ready; observed invoice navigation is read only", async () => {
  const f=await fixture(), id=await precheck(f), p=permit(id); await f.c.handle(command("SUBMIT_ORDER",f.identity,{permit:p}));
  const ctx={submission_nonce:p.nonce,submitted_at:p.issued_at,order_id:null,invoice_id:null};
  const receipt={ok:true,stage:"ORDER_CREATED",code:"ORDER_FOUND",outcome:"ORDER_FOUND",login:"VALID",challenge:"NONE",order_id:"501",invoice_id:"601",amount_cents:product.cents,currency:"USD",billing:"annually",product_verified:true,created_at:p.issued_at,observed_at:new Date().toISOString(),official_url:"https://bandwagonhost.com/viewinvoice.php?id=601"};
  f.page.receipt=receipt; await f.c.handle(command("RECONCILE_ORDER",f.identity,ctx));
  assert.deepEqual(f.navigations,[receipt.official_url]);
  f.page.receipt={...receipt,stage:"PAYMENT_READY",outcome:"PAYMENT_READY",unpaid:true};
  await f.c.handle(command("RECONCILE_ORDER",f.identity,{...ctx,order_id:"999",invoice_id:"601"}));
  assert.equal(f.events.findLast(e=>e.type==="ORDER_OBSERVATION").payload.outcome,"UNKNOWN");
  await f.c.handle(command("RECONCILE_ORDER",f.identity,{...ctx,order_id:"501",invoice_id:"601"}));
  assert.equal(f.events.findLast(e=>e.type==="ORDER_OBSERVATION").payload.outcome,"PAYMENT_READY");
  assert.equal(f.c.status().mutation_uncertain,false);
  assert.deepEqual(f.actions,["submitOrder"]);
  const {ok: _ok,...publicReceipt}=receipt;
  const event={...command("ORDER_PRECHECK",f.identity),type:"ORDER_OBSERVATION",payload:{...publicReceipt,tab_id:10,outcome:"PAYMENT_READY",unpaid:true}};
  assert.equal(validateEnvelope(event,"event"),event);
  assert.throws(()=>validateEnvelope({...event,payload:{...event.payload,unpaid:false}},"event"));
  assert.throws(()=>validateEnvelope({...event,payload:{...event.payload,official_url:"https://bandwagonhost.com/viewinvoice.php?id=602"}},"event"));
});
