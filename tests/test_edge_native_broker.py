"""Durable correlation, interruption and stop boundaries; no merchant traffic."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from uuid import uuid4

from autograb.core.models import Product
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import ProtocolError, make_message
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore


class EdgeBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.broker = EdgeBroker(self.store)
        self.connection_id = str(uuid4())
        self.ready()
        self.intent = self.create()
        self.payload = {"mode": "DRY_RUN", "product": {"name": "Fixture", "url": "https://bandwagonhost.com/order/ecommerce",
                                                       "period": "annually", "cents": 4999, "currency": "USD"}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ready(self, command_id=None):
        return self.broker.handle_event(make_message("EDGE_READY", command_id=command_id or self.connection_id,
                                                     payload={"version": "0.2.1"}))

    def create(self, product_id="87", *, simulated=False):
        product = Product(product_id=product_id, name="Fixture", availability="AVAILABLE", prices=[],
                          product_url="https://bandwagonhost.com/order/ecommerce", eligible=True)
        event = self.store.create_event(product, "PRODUCT_CHANGED", simulated=simulated)
        return self.broker.intents.create(event, product)

    def start(self):
        self.command = self.broker.enqueue("START_DRY_RUN", self.intent["intent_id"], self.intent["product_id"], self.payload)
        self.assertEqual(self.broker.next_command()["command_id"], self.command["command_id"])
        return self.command

    def event(self, kind, **payload):
        defaults = {"tab_id": 12, "challenge": "NONE", "login": "VALID"}
        defaults.update(payload)
        if kind == "PAGE_OPENED":
            defaults.setdefault("url", "https://bandwagonhost.com/order/ecommerce")
        return make_message(kind, intent_id=self.intent["intent_id"], product_id=self.intent["product_id"],
                            command_id=self.command["command_id"], payload=defaults)

    def send(self, kind, **payload):
        return self.broker.handle_event(self.event(kind, **payload))

    def to_cart(self):
        self.start()
        self.send("PAGE_OPENED")
        self.send("PRODUCT_VERIFIED")
        self.send("CART_READY", cart_id="0")

    def test_handshake_status_has_no_live_authority(self):
        status = self.broker.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["installed"], "OBSERVED")
        self.assertEqual(status["version"], "0.2.1")
        self.assertEqual(status["live"], "OFF")
        self.assertTrue(status["disarmed"])

    def test_second_connection_cannot_replace_live_host(self):
        with self.assertRaisesRegex(ProtocolError, "CONNECTION_CONFLICT"):
            self.ready(str(uuid4()))

    def test_ping_reply_must_match_dispatched_command(self):
        ping = self.broker.enqueue("PING")
        with self.assertRaises(ProtocolError):
            self.ready(ping["command_id"])
        self.broker.next_command()
        self.ready(ping["command_id"])
        with self.assertRaises(ProtocolError):
            self.ready(ping["command_id"])

    def test_checkout_sequence_updates_existing_intent_atomically_without_order(self):
        self.to_cart()
        self.send("CHECKOUT_READY")
        saved = self.broker.intents.get(self.intent["intent_id"])
        self.assertEqual(saved["state"], "CHECKOUT_READY")
        self.assertEqual(saved["edge_checkpoint"], "CHECKOUT_READY")
        for field in ("order_id", "invoice_id", "payment_url", "submit_started_at"):
            self.assertIsNone(saved[field])
        self.assertFalse(saved["payment_page_verified"])

    def test_checkout_needs_cart_login_and_normal_page_evidence(self):
        self.start()
        with self.assertRaises(ProtocolError):
            self.send("CHECKOUT_READY")
        self.send("PAGE_OPENED")
        self.send("PRODUCT_VERIFIED")
        with self.assertRaises(ProtocolError):
            self.send("CART_READY")
        self.send("CART_READY", cart_id="0")
        for extra in ({"login": "UNKNOWN"}, {"challenge": "REQUIRED"}):
            with self.assertRaises(ProtocolError):
                self.send("CHECKOUT_READY", **extra)
        self.assertEqual(self.broker.intents.get(self.intent["intent_id"])["state"], "CART_READY")

    def test_explicit_cart_rebuild_reverifies_original_product_and_cart_only(self):
        self.start()
        self.send("PAGE_OPENED")
        with self.assertRaisesRegex(ProtocolError, "CART_REBUILD_NOT_ALLOWED"):
            self.send("PAGE_OPENED", code="CART_REBUILDING")
        self.send("PRODUCT_VERIFIED")
        self.send("CART_READY", cart_id="configuration_0")
        with self.assertRaisesRegex(ProtocolError, "CART_REBUILD_NOT_ALLOWED"):
            self.send("PAGE_OPENED", code="CART_REBUILDING")
        self.send("SITE_CHANGED", code="EMPTY_CART")
        self.send("PAGE_OPENED")
        self.command = self.broker.enqueue("RESUME_INTENT", self.intent["intent_id"], "87", self.payload)
        self.broker.next_command()
        for unsafe in ({"challenge": "REQUIRED"}, {"login": "REQUIRED"}):
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(ProtocolError, "CART_REBUILD_NOT_ALLOWED"):
                self.send("PAGE_OPENED", code="CART_REBUILDING", **unsafe)
        self.send("PAGE_OPENED", code="CART_REBUILDING")
        saved = self.broker.intents.get(self.intent["intent_id"])
        self.assertEqual((saved["state"], saved["edge_checkpoint"], saved["cart_identifier"]),
                         ("CART_READY", "PAGE_OPENED", "configuration_0"))
        self.send("PRODUCT_VERIFIED")
        with self.assertRaisesRegex(ProtocolError, "CART_IDENTITY_MISMATCH"):
            self.send("CART_READY", cart_id="configuration_1")
        self.send("CART_READY", cart_id="configuration_0")
        self.send("CHECKOUT_READY")
        self.assertEqual(len(self.broker.intents.list()), 1)
        self.assertEqual(self.broker.intents.get(self.intent["intent_id"])["state"], "CHECKOUT_READY")

    def test_wrong_intent_product_command_and_tab_rejected_without_state_change(self):
        self.start()
        self.send("PAGE_OPENED")
        for key, value in (("intent_id", str(uuid4())), ("product_id", "88"), ("command_id", str(uuid4()))):
            event = self.event("PRODUCT_VERIFIED")
            event[key] = value
            with self.subTest(key=key), self.assertRaises(ProtocolError):
                self.broker.handle_event(event)
        with self.assertRaises(ProtocolError):
            self.send("PRODUCT_VERIFIED", tab_id=13)
        self.assertEqual(self.broker.intents.get(self.intent["intent_id"])["edge_checkpoint"], "PAGE_OPENED")

    def test_replayed_message_rejected_and_never_redispatched(self):
        self.start()
        event = self.event("PAGE_OPENED")
        self.broker.handle_event(event)
        with self.assertRaisesRegex(ProtocolError, "MESSAGE_REPLAYED"):
            self.broker.handle_event(event)
        self.assertIsNone(self.broker.next_command())

    def test_live_commands_and_receipt_assertions_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "LIVE_NOT_ENABLED"):
            self.broker.enqueue("START_CHECKOUT", self.intent["intent_id"], "87", self.payload)
        self.start()
        for kind in ("ORDER_CREATED", "INVOICE_FOUND", "PAYMENT_READY"):
            with self.assertRaisesRegex(ProtocolError, "LIVE_NOT_ENABLED"):
                self.send(kind)

    def test_single_active_edge_intent_even_for_different_product(self):
        self.start()
        other = self.create("88")
        with self.assertRaisesRegex(ProtocolError, "ANOTHER_EDGE_INTENT_ACTIVE"):
            self.broker.enqueue("START_DRY_RUN", other["intent_id"], "88", self.payload)

    def test_persisted_order_or_payment_evidence_blocks_pre_submit_intent_execution(self):
        evidence = {
            "order_id": "123", "invoice_id": "456",
            "submit_started_at": "2026-09-22T00:00:00+00:00",
            "payment_url": "https://bandwagonhost.com/viewinvoice.php?id=456",
            "payment_page_verified": 1,
            "verification_json": json.dumps({"payment_page_verified": True}),
        }
        for state in ("INTENT_CREATED", "CART_READY", "CHECKOUT_READY"):
            for field, value in evidence.items():
                with self.subTest(state=state, field=field):
                    # Persisted receipt evidence wins even if a stale state label
                    # incorrectly still suggests that cart work could be resumed.
                    with self.store._transaction():
                        self.store.connection.execute("""UPDATE purchase_intents SET state=?,
                            order_id=NULL,invoice_id=NULL,submit_started_at=NULL,payment_url=NULL,
                            payment_page_verified=0,verification_json=NULL WHERE intent_id=?""",
                            (state, self.intent["intent_id"]))
                        self.store.connection.execute(
                            f"UPDATE purchase_intents SET {field}=? WHERE intent_id=?",
                            (value, self.intent["intent_id"]))
                    with self.assertRaisesRegex(ProtocolError, "^INTENT_IDENTITY_REJECTED$"):
                        self.broker.enqueue("RESUME_INTENT", self.intent["intent_id"], "87", self.payload)
                    self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM edge_commands").fetchone()[0], 0)

    def test_simulation_cannot_control_real_edge(self):
        simulated = self.create("88", simulated=True)
        with self.assertRaisesRegex(ProtocolError, "INTENT_IDENTITY_REJECTED"):
            self.broker.enqueue("START_DRY_RUN", simulated["intent_id"], "88", self.payload)

    def test_initial_command_cannot_be_requeued(self):
        self.start()
        with self.assertRaisesRegex(ProtocolError, "INTENT_ALREADY_STARTED"):
            self.broker.enqueue("START_DRY_RUN", self.intent["intent_id"], "87", self.payload)

    def test_open_only_observations_can_repeat_before_explicit_dry_run(self):
        self.command = self.broker.enqueue("OPEN_PRODUCT", self.intent["intent_id"], "87", self.payload)
        self.broker.next_command()
        self.send("PAGE_OPENED")
        self.send("PAGE_OPENED")
        self.assertEqual(self.broker.status()["execution_state"], "OPENED")
        self.start()
        self.send("PRODUCT_VERIFIED")

    def test_cancel_ack_keeps_cancelled_status_without_releasing_product_occupancy(self):
        self.to_cart()
        self.command = self.broker.enqueue("CANCEL_INTENT", self.intent["intent_id"], "87")
        self.broker.next_command()
        self.send("FAILED", code="CANCELLED")
        self.assertEqual(self.broker.status()["execution_state"], "CANCELLED")
        self.assertIsNotNone(self.broker.intents.active_for_product("87"))

    def test_challenge_pauses_and_email_claims_once_across_restart(self):
        self.start()
        self.send("PAGE_OPENED")
        first = self.send("HUMAN_CHALLENGE_REQUIRED", challenge="REQUIRED", mutation_uncertain=True)
        self.assertIsNotNone(first["human_notification"])
        second = self.send("HUMAN_CHALLENGE_REQUIRED", challenge="REQUIRED")
        self.assertIsNone(second["human_notification"])
        self.store.close()
        self.store = Store(self.path)
        self.broker = EdgeBroker(self.store)
        third = self.send("HUMAN_CHALLENGE_REQUIRED", challenge="REQUIRED")
        self.assertIsNone(third["human_notification"])
        saved = self.broker.intents.get(self.intent["intent_id"])
        self.assertEqual(saved["edge_state"], "WAITING_FOR_HUMAN")
        self.assertTrue(saved["edge_mutation_uncertain"])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1)

    def test_human_pause_blocks_all_action_events_until_normal_page_and_explicit_resume(self):
        self.start()
        self.send("PAGE_OPENED")
        self.send("HUMAN_CHALLENGE_REQUIRED", challenge="REQUIRED")
        with self.assertRaises(ProtocolError):
            self.send("PRODUCT_VERIFIED")
        with self.assertRaisesRegex(ProtocolError, "NORMAL_PAGE_NOT_VERIFIED"):
            self.broker.enqueue("RESUME_INTENT", self.intent["intent_id"], "87", self.payload)
        self.send("PAGE_OPENED", challenge="NONE")
        self.assertTrue(self.broker.status()["resume_available"])
        changed = deepcopy(self.payload)
        changed["product"]["cents"] += 1
        with self.assertRaisesRegex(ProtocolError, "RESUME_PRODUCT_CHANGED"):
            self.broker.enqueue("RESUME_INTENT", self.intent["intent_id"], "87", changed)
        self.command = self.broker.enqueue("RESUME_INTENT", self.intent["intent_id"], "87", self.payload)
        self.broker.next_command()
        self.send("PAGE_OPENED")
        self.send("PRODUCT_VERIFIED")
        self.send("CART_READY", cart_id="0")
        self.send("CHECKOUT_READY")
        self.assertEqual(len(self.broker.intents.list()), 1)

    def test_disconnect_halts_pending_commands_and_reconnect_is_read_only(self):
        self.start()
        self.send("PAGE_OPENED")
        self.broker.disconnect()
        with self.assertRaises(ProtocolError):
            self.send("PRODUCT_VERIFIED")
        self.connection_id = str(uuid4())
        self.ready()
        self.assertIsNone(self.broker.next_command())
        self.assertEqual(self.broker.status()["execution_state"], "DISCONNECTED")
        self.send("PAGE_OPENED")
        self.assertTrue(self.broker.status()["resume_available"])
        self.assertEqual(self.broker.intents.get(self.intent["intent_id"])["state"], "INTENT_CREATED")

    def test_disarm_halts_intent_before_disarm_delivery(self):
        self.start()
        disarm = self.broker.enqueue("DISARM")
        with self.assertRaises(ProtocolError):
            self.send("CART_READY", cart_id="0")
        self.assertEqual(self.broker.next_command()["command_id"], disarm["command_id"])
        self.assertEqual(self.broker.status()["execution_state"], "DISARMED")

    def test_cancel_never_cancels_order_or_marks_intent_reusable(self):
        self.to_cart()
        self.broker.enqueue("CANCEL_INTENT", self.intent["intent_id"], "87")
        saved = self.broker.intents.get(self.intent["intent_id"])
        self.assertEqual(saved["state"], "CART_READY")
        self.assertEqual(saved["edge_state"], "CANCELLED")
        self.assertIsNotNone(self.broker.intents.active_for_product("87"))

    def test_migration_preserves_existing_baseline_and_intent_data(self):
        before = self.store.summary()
        original = self.broker.intents.get(self.intent["intent_id"])
        EdgeBroker(self.store)
        self.assertEqual(self.store.summary(), before)
        self.assertEqual(self.broker.intents.get(self.intent["intent_id"]), original)
        tables = {row[0] for row in self.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("edge_orders", tables)

    def test_only_structured_error_code_is_persisted_for_diagnosis(self):
        self.start()
        self.send("SITE_CHANGED", code="CART_PRODUCT_MISMATCH")
        self.assertEqual(self.broker.status()["error_code"], "CART_PRODUCT_MISMATCH")
        with self.assertRaises(ProtocolError):
            self.send("SITE_CHANGED", code="Account private text")
        self.assertEqual(self.broker.status()["error_code"], "CART_PRODUCT_MISMATCH")

    def test_actual_javascript_controller_events_pass_python_broker_sequence(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is needed for the controller/native integration test")
        self.start()
        script = """import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
const {CompanionController} = await import(process.argv[1]);
const {command, connectionId} = JSON.parse(readFileSync(0,'utf8'));
const events=[], actions=[], saved={}; let stage='PRODUCT', idCount=0, documentId='document-1', period='monthly';
const result=()=>({ok:true,stage,code:'VERIFIED',challenge:'NONE',login:'VALID',
  ...(['CART','CONFIGURATION'].includes(stage)?{cart_id:'configuration_0'}:{})});
const api={storage:{local:{async get(k){return {[k]:saved[k]};},async set(v){Object.assign(saved,structuredClone(v));}}},
 tabs:{async create(){return{id:12};},async get(){return{id:12,windowId:1,status:'complete',
  url:stage==='PRODUCT'?command.payload.product.url:'https://bandwagonhost.com/cart.php?a='+
  (stage==='CART'?'view':stage==='CHECKOUT'?'checkout':'confproduct')};},
 async update(){},async sendMessage(id,request,options){
  if(options?.documentId!==documentId)throw Error('old document is gone');
  if(request.ticket){actions.push(request.operation);
   if(['addToCart','openCheckout'].includes(request.operation)||request.operation==='configureProduct'&&stage==='PRODUCT')documentId+='-next';
   if(request.operation==='selectBillingPeriod')period=request.expected.period;
   if(request.operation==='configureProduct')stage='CONFIGURATION';
   if(request.operation==='addToCart')stage='CART';
   if(request.operation==='openCheckout')stage='CHECKOUT';}
  if(request.operation==='verifyProduct'&&stage==='PRODUCT'&&period!==request.expected.period)return{...result(),ok:false,code:'BILLING_MISMATCH'};
  return {...result(),...(request.ticket?{action:'NAVIGATED'}:{})};}},
 windows:{async update(){}},scripting:{async executeScript(){return[{frameId:0,documentId}];}}};
const controller=new CompanionController(api,{uuid:()=>idCount++===0?connectionId:randomUUID()});
await controller.restore();await controller.attach({postMessage:e=>events.push(e)});
await controller.handle(command);for(let i=0;i<16;i++)await controller.pump();
process.stdout.write(JSON.stringify({events,actions,period,state:controller.active.state}));"""
        module = (Path(__file__).resolve().parents[1] / "edge-extension/service-worker.js").as_uri()
        completed = subprocess.run([node, "--input-type=module", "-e", script, module], input=json.dumps({
            "command": self.command, "connectionId": self.connection_id}), text=True, capture_output=True, check=True)
        result = json.loads(completed.stdout)
        for event in result["events"]:
            self.broker.handle_event(event)
        self.assertEqual(result["state"], "CHECKOUT_READY")
        self.assertEqual(result["period"], "annually")
        self.assertEqual(result["actions"], ["selectBillingPeriod", "configureProduct", "configureProduct", "addToCart", "openCheckout"])
        self.assertEqual(result["actions"].count("addToCart"), 1)
        self.assertEqual(self.broker.intents.get(self.intent["intent_id"])["state"], "CHECKOUT_READY")


if __name__ == "__main__":
    unittest.main()
