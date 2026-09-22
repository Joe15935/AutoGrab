import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import {readFile} from "node:fs/promises";
import {randomUUID} from "node:crypto";
const source = await readFile(new URL("../content.js", import.meta.url), "utf8");
const expected = {name: "20G KVM - PROMO", url: "https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9", period: "annually", cents: 4999, currency: "USD", product_id: "87"};
function fixture() {
  let listener, writes = 0, required = false;
  const context = vm.createContext({URL, location: {origin: "https://bandwagonhost.com"}, chrome: {runtime: {id: "own-extension", onMessage: {addListener(f) {listener = f;}}}}, AutoGrabBandwagon: {
    async detectChallenge() {return {challenge: required ? "REQUIRED" : "NONE"};},
    async detectPage() {return {ok: true, stage: "CONFIGURATION", login: "VALID", challenge: "NONE", private_field: "must not escape"};},
    async verifyProduct() {return {ok: true, cart_id: "configuration_0"};},
    async addToCart() {writes++; return {ok: true, stage: "CART", code: "DISPATCHED", action: "SUBMITTED"};},
  }});
  vm.runInContext(source, context);
  return {context, writes: () => writes, challenge: value => required = value, send(request, sender = {id: "own-extension"}) {
    return new Promise(resolve => {const handled = listener(request, sender, response => resolve({handled:true, response})); if (!handled) resolve({handled:false});});
  }};
}
const request = (operation="addToCart") => ({source:"AUTOGRAB_COMPANION", operation, expected, ticket:randomUUID(), cart_id:"configuration_0"});
test("content refuses arbitrary operation and external sender", async () => {
  const f=fixture(); assert.equal((await f.send(request("evaluate"))).handled,false); assert.equal((await f.send(request(),{id:"other-extension"})).handled,false); assert.equal(f.writes(),0);
});
test("content rejects additional instruction fields", async () => {const f=fixture(); assert.equal((await f.send({...request(), script:"anything"})).handled,false);});
test("challenge prevents all DOM action and proves NONE", async () => {
  const f=fixture(); f.challenge(true); const result=await f.send(request()); assert.equal(result.response.challenge,"REQUIRED"); assert.equal(result.response.action,"NONE"); assert.equal(f.writes(),0);
});
test("mutation ticket cannot execute twice", async () => {const f=fixture(), r=request(); await f.send(r); const second=await f.send(r); assert.equal(second.response.code,"MUTATION_TICKET_REJECTED"); assert.equal(f.writes(),1);});
test("reinjection preserves mutation tombstone", async () => {const f=fixture(),r=request();await f.send(r);vm.runInContext(source,f.context);await f.send(r);assert.equal(f.writes(),1);});
test("content read strips unknown private fields", async () => {const f=fixture();const result=await f.send(request("detectPage"));assert.equal(result.response.private_field,undefined);assert.equal(result.response.stage,"CONFIGURATION");});
test("content rechecks bound cart before any action",async()=>{
  const f=fixture();const result=await f.send({...request(),cart_id:"configuration_1"});assert.equal(result.response.code,"CART_ID_CHANGED");assert.equal(result.response.action,"NONE");assert.equal(f.writes(),0);
});
test("content refuses cart action without bound cart",async()=>{const f=fixture();const result=await f.send({...request(),cart_id:null});assert.equal(result.response.code,"CART_ID_UNVERIFIED");assert.equal(f.writes(),0);});
test("new provider content cannot dispatch even with an unused valid ticket", async()=>{
  let listener, writes=0;
  const context=vm.createContext({URL,location:{origin:"https://www.dmit.io"},chrome:{runtime:{id:"own-extension",onMessage:{addListener(f){listener=f;}}}},AutoGrabDMIT:{
    async detectChallenge(){return {challenge:"NONE"};},async addToCart(){writes++;return {ok:true};},
  }});
  vm.runInContext(source,context);
  const r={...request(),provider:"dmit",expected:{...expected,url:"https://www.dmit.io/cart.php?gid=1"}};
  const response=await new Promise(resolve=>listener(r,{id:"own-extension"},resolve));
  assert.equal(response.code,"ADAPTER_READ_ONLY");assert.equal(response.action,"NONE");assert.equal(writes,0);
});
