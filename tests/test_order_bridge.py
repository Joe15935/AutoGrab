"""Offline transport-to-ledger checks for one-shot order authorization."""
import asyncio
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from autograb.core.live import signal_disarm
from autograb.core.lock import ProcessLock, lock_is_held, submission_lease_path
from autograb.core.models import Product
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import ProtocolError,make_message,validate
from autograb.providers.edge_order import EdgeOrderProvider
from autograb.storage.database import Store


def stamp():return datetime.now(timezone.utc).isoformat()


class OrderBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'state.sqlite3')
        self.broker=EdgeBroker(self.store)
        self.broker.handle_event(make_message('EDGE_READY',payload={'version':'0.5.0'}))
        self.product=Product('266','Synthetic order bridge','AVAILABLE',[{'cents':7990,'currency':'USD','period':'monthly','available':True}],
                             'https://www.dmit.io/cart.php',provider='dmit',eligible=True)
        event=self.store.create_event(self.product,'SIMULATED',simulated=True)
        self.intent=self.broker.intents.create(event,self.product)
        self.identity=self.intent['intent_id']
        self.broker.intents.set_cart(self.identity,'configuration_0');self.broker.intents.mark_checkout_ready(self.identity)
        self.payload={'mode':'DRY_RUN','tab_id':22,'product':{'name':self.product.name,'url':self.product.product_url,'cents':7990,'currency':'USD','period':'monthly'}}

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def precheck(self):
        cmd=self.broker.enqueue_order('ORDER_PRECHECK',self.identity,self.payload)
        self.assertEqual(self.broker.next_command()['command_id'],cmd['command_id'])
        observation={'tab_id':22,'stage':'ORDER_PRECHECK','code':'ORDER_PRECHECK_VERIFIED','login':'VALID','challenge':'NONE',
            'outcome':'PRECHECK_READY','precheck_id':str(uuid4()),'observed_at':stamp(),'amount_cents':7990,'currency':'USD','billing':'monthly',
            'product_verified':True,'no_charge_verified':True}
        response=make_message('ORDER_OBSERVATION',provider='dmit',intent_id=self.identity,product_id='266',command_id=cmd['command_id'],payload=observation)
        self.broker.handle_event(response);self.broker.intents.set_order_precheck(self.identity,observation)
        return observation,response

    def submit(self, *, enqueue=True):
        check,_=self.precheck();issued=stamp()
        permit={'nonce':str(uuid4()),'precheck_id':check['precheck_id'],'issued_at':issued,
                'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat(),'real_order_smoke_test_armed':True}
        self.assertTrue(self.broker.intents.begin_order_submission(self.identity,permit['nonce'],check['precheck_id'],submitted_at=issued))
        payload={**self.payload,'mode':'REAL_ORDER_SMOKE_TEST','permit':permit}
        if not enqueue:
            return None,payload
        lease=ProcessLock(submission_lease_path(Path(self.tmp.name),permit['nonce']))
        lease.__enter__();self.addCleanup(lease.__exit__)
        self.lease=lease
        cmd=self.broker.enqueue_order('SUBMIT_ORDER',self.identity,payload)
        return cmd,payload

    def test_uncommitted_or_mismatched_submit_cannot_enter_queue(self):
        check,_=self.precheck()
        p={**self.payload,'mode':'REAL_ORDER_SMOKE_TEST','permit':{'nonce':str(uuid4()),'precheck_id':check['precheck_id'],'issued_at':stamp(),
            'expires_at':(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat(),'real_order_smoke_test_armed':True}}
        with self.assertRaises(ProtocolError):self.broker.enqueue_order('SUBMIT_ORDER',self.identity,p)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM edge_commands WHERE type='SUBMIT_ORDER'").fetchone()[0],0)

    def test_persist_before_dispatch_disconnect_replay_and_reconnect_never_resend(self):
        cmd,payload=self.submit()
        self.assertIsNotNone(self.broker.intents.get(self.identity)['submit_started_at'])
        self.assertEqual(self.broker.next_command()['command_id'],cmd['command_id']);self.assertIsNone(self.broker.next_command())
        self.broker.disconnect()
        self.assertEqual(self.broker.intents.get(self.identity)['state'],'ORDER_UNCERTAIN')
        self.broker.handle_event(make_message('EDGE_READY',payload={'version':'0.5.0'}))
        self.assertIsNone(self.broker.next_command())
        with self.assertRaises(ProtocolError):self.broker.enqueue_order('SUBMIT_ORDER',self.identity,payload)

    def test_kill_after_persistence_stops_native_dispatch(self):
        self.submit();signal_disarm(Path(self.tmp.name))
        self.assertIsNone(self.broker.next_command())
        self.assertEqual(self.broker.intents.get(self.identity)['state'],'ORDER_UNCERTAIN')

    def test_cancelled_wait_halts_queue_and_releases_submission_lease(self):
        _,payload=self.submit(enqueue=False)
        permit=payload['permit']
        provider=EdgeOrderProvider(self.store,'dmit',22)
        lease_path=submission_lease_path(Path(self.tmp.name),permit['nonce'])

        async def cancel_queued():
            task=asyncio.create_task(provider.submit_order(self.broker.intents.get(self.identity),self.product,permit))
            try:
                # _exchange enqueues synchronously before its first yield.
                await asyncio.sleep(0)
                row=self.store.connection.execute("SELECT command_id,status FROM edge_commands WHERE type='SUBMIT_ORDER'").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row['status'],'QUEUED')
                self.assertTrue(lock_is_held(lease_path))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                return row['command_id']
            finally:
                if not task.done():
                    task.cancel()
                    try:await task
                    except asyncio.CancelledError:pass

        command_id=asyncio.run(cancel_queued())
        self.assertEqual(self.broker.order_result(command_id)['status'],'HALTED')
        self.assertFalse(lock_is_held(lease_path))
        self.assertIsNone(self.broker.next_command())
        # The owner marks the possibly dispatched purchase uncertain; neither
        # cancellation nor this transition returns its one-use nonce.
        self.broker.intents.mark_uncertain(self.identity)
        saved=self.broker.intents.get(self.identity)
        self.assertEqual((saved['state'],saved['submission_nonce']),('ORDER_UNCERTAIN',permit['nonce']))

    def test_uncertain_primary_state_blocks_dispatch_even_with_live_lease(self):
        command,payload=self.submit()
        self.assertTrue(lock_is_held(submission_lease_path(Path(self.tmp.name),payload['permit']['nonce'])))
        self.broker.intents.mark_uncertain(self.identity)
        self.assertIsNone(self.broker.next_command())
        self.assertEqual(self.broker.order_result(command['command_id'])['status'],'HALTED')
        self.assertEqual(self.broker.intents.get(self.identity)['submission_nonce'],payload['permit']['nonce'])

    def test_authorizer_process_exit_releases_lease_and_stale_file_cannot_dispatch(self):
        command,payload=self.submit()
        path=submission_lease_path(Path(self.tmp.name),payload['permit']['nonce'])
        self.lease.__exit__()
        self.assertFalse(lock_is_held(path))
        script="""import os,sys
from pathlib import Path
from autograb.core.lock import ProcessLock
lease=ProcessLock(Path(sys.argv[1]));lease.__enter__()
print('LOCKED',flush=True)
sys.stdin.readline()
os._exit(0)  # No Python cleanup: the kernel must release ownership.
"""
        child=subprocess.Popen([sys.executable,'-u','-c',script,str(path)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=Path(__file__).resolve().parents[1])
        try:
            self.assertEqual(child.stdout.readline().strip(),'LOCKED')
            self.assertTrue(lock_is_held(path))
            _,stderr=child.communicate('exit\n',timeout=5)
            self.assertEqual(child.returncode,0,stderr)
        finally:
            if child.poll() is None:
                child.kill();child.communicate(timeout=5)
        self.assertTrue(path.exists())
        self.assertFalse(lock_is_held(path))
        self.assertIsNone(self.broker.next_command())
        self.assertEqual(self.broker.order_result(command['command_id'])['status'],'HALTED')
        self.assertEqual(self.broker.intents.get(self.identity)['state'],'ORDER_UNCERTAIN')

    def test_read_reply_is_correlated_one_shot_and_cannot_claim_payment(self):
        check,response=self.precheck()
        with self.assertRaises(ProtocolError):self.broker.handle_event(response)
        wrong=deepcopy(response);wrong['message_id']=str(uuid4());wrong['provider']='bandwagon'
        with self.assertRaises(ProtocolError):self.broker.handle_event(wrong)
        self.assertIsNone(self.broker.intents.get(self.identity)['order_id'])
        self.assertFalse(self.broker.intents.get(self.identity)['payment_page_verified'])

    def test_reconciliation_requires_exact_known_ids_quote_time_and_official_unpaid_page(self):
        cmd,payload=self.submit();self.broker.next_command();self.broker.disconnect()
        self.broker.handle_event(make_message('EDGE_READY',payload={'version':'0.5.0'}))
        saved=self.broker.intents.get(self.identity)
        rp={**self.payload,'submission_nonce':saved['submission_nonce'],'submitted_at':saved['submit_started_at'],'order_id':None,'invoice_id':None}
        command=self.broker.enqueue_order('RECONCILE_ORDER',self.identity,rp);self.broker.next_command()
        observation={'tab_id':22,'stage':'PAYMENT_READY','code':'PAYMENT_READY_VERIFIED','login':'VALID','challenge':'NONE','outcome':'PAYMENT_READY',
            'order_id':'100','invoice_id':'200','official_url':'https://www.dmit.io/viewinvoice.php?id=200','unpaid':True,
            'product_verified':True,'amount_cents':7990,'currency':'USD','billing':'monthly','observed_at':stamp(),'created_at':stamp()}
        observation['observed_at']=stamp()
        for field,value in [('official_url','https://www.dmit.io/cart.php?a=checkout'),('unpaid',False),('amount_cents',1),('created_at','2000-01-01T00:00:00+00:00')]:
            bad={**observation,field:value}
            with self.subTest(field=field),self.assertRaises(ProtocolError):
                event=make_message('ORDER_OBSERVATION',provider='dmit',intent_id=self.identity,product_id='266',command_id=command['command_id'],payload=bad)
                self.broker.handle_event(event)
        event=make_message('ORDER_OBSERVATION',provider='dmit',intent_id=self.identity,product_id='266',command_id=command['command_id'],payload=observation)
        self.broker.handle_event(event)
        self.assertEqual(self.broker.order_result(command['command_id'])['observation'],observation)
        # Transport evidence alone does not write PAYMENT_READY; shared Core validates it.
        self.assertFalse(self.broker.intents.get(self.identity)['payment_page_verified'])

    def test_python_and_js_accept_same_read_submit_and_invoice_contract(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node unavailable')
        cmd,payload=self.submit()
        reconcile=make_message('RECONCILE_ORDER',provider='dmit',intent_id=self.identity,product_id='266',payload={**self.payload,
            'submission_nonce':payload['permit']['nonce'],'submitted_at':payload['permit']['issued_at'],'order_id':None,'invoice_id':None})
        script="import {validateEnvelope} from './edge-extension/protocol.js'; let input='';for await(const c of process.stdin)input+=c; for(const m of JSON.parse(input))validateEnvelope(m,'command');"
        run=subprocess.run([node,'--input-type=module','-e',script],input=json.dumps([cmd,reconcile]),text=True,capture_output=True,cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(run.returncode,0,run.stderr)
