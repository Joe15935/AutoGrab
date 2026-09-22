"""Default CLI authorization boundaries; temporary ledger and mocked I/O only."""
import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from autograb.core.config import Config
from autograb.core.errors import AutoGrabError
from autograb.core.live import RealOrderSmokeGuard
from autograb.core.models import Product
from autograb.order_cli import register_commands, run_order
from autograb.providers.edge_order import EdgeOrderProvider
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore


class OrderCLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.config=Config(self.root)
        self.parser=argparse.ArgumentParser()
        register_commands(self.parser.add_subparsers(dest='command',required=True))

    def args(self, command, identity, *flags):
        return self.parser.parse_args([command,'--intent-id',identity,'--tab-id','22',*flags])

    async def test_missing_either_explicit_smoke_switch_stops_before_any_side_effect(self):
        for flags in ((), ('--mode','LIVE'), ('--real-order-smoke-test-armed',),
                      ('--mode','DRY_RUN','--real-order-smoke-test-armed')):
            with self.subTest(flags=flags), ExitStack() as stack:
                for name in ('ProcessLock','Store','EdgeOrderProvider','EmailNotifier','load_setup','RealOrderSmokeGuard','PurchaseRunner'):
                    blocked=stack.enter_context(patch('autograb.order_cli.'+name,side_effect=AssertionError('unarmed command reached '+name)))
                    stack.callback(blocked.assert_not_called)
                output=stack.enter_context(redirect_stdout(StringIO()))
                result=await run_order(self.args('order-smoke',str(uuid4()),*flags),self.config)
                data=json.loads(output.getvalue())
                self.assertEqual((result,data['status']),(2,'REAL_ORDER_SMOKE_TEST_NOT_ARMED'))
                self.assertFalse(data['REAL_ORDER_SMOKE_TEST_ARMED'])
                self.assertEqual((data['LIVE'],data['automatic_payment']),('OFF','DISABLED'))
                self.assertFalse((self.root/'data').exists())

    async def test_invalid_or_missing_intent_is_explicit_before_edge_or_email(self):
        for identity in ('invalid-intent',str(uuid4())):
            with self.subTest(identity=identity), ExitStack() as stack:
                for name in ('EdgeOrderProvider','EmailNotifier','load_setup','RealOrderSmokeGuard','PurchaseRunner'):
                    stack.enter_context(patch('autograb.order_cli.'+name,side_effect=AssertionError('missing intent reached '+name)))
                with self.assertRaises(AutoGrabError) as raised:
                    await run_order(self.args('order-precheck',identity),self.config)
                self.assertEqual(raised.exception.code,'INTENT_NOT_FOUND')

    async def test_readonly_precheck_and_human_pause_never_send_mail_or_acquire_permit(self):
        product=Product('266','Synthetic CLI checkout','AVAILABLE',
            [{'period':'monthly','cents':7990,'currency':'USD','available':True}],
            'https://www.dmit.io/cart.php',provider='dmit',eligible=True)
        path=self.root/'data/autograb.sqlite3'
        with Store(path) as store:
            intents=IntentStore(store)
            event=store.create_event(product,'SIMULATED',simulated=True)
            identity=intents.create(event,product)['intent_id']
            intents.set_cart(identity,'configuration_0');intents.mark_checkout_ready(identity)
        ready={'tab_id':22,'stage':'ORDER_PRECHECK','code':'ORDER_PRECHECK_VERIFIED','login':'VALID','challenge':'NONE',
            'outcome':'PRECHECK_READY','precheck_id':str(uuid4()),'observed_at':datetime.now(timezone.utc).isoformat(),
            'amount_cents':7990,'currency':'USD','billing':'monthly','product_verified':True,'no_charge_verified':True}
        human={'tab_id':22,'stage':'ORDER_PRECHECK','code':'ORDER_SESSION_UNVERIFIED',
            'login':'REQUIRED','challenge':'NONE','outcome':'LOGIN_REQUIRED'}
        provider=Mock(spec=EdgeOrderProvider)
        provider.precheck_order=AsyncMock(side_effect=[ready,human])
        notifier=Mock()
        with ExitStack() as stack:
            stack.enter_context(patch('autograb.order_cli.EdgeOrderProvider',return_value=provider))
            stack.enter_context(patch('autograb.order_cli.load_setup',return_value=object()))
            stack.enter_context(patch('autograb.order_cli.EmailNotifier',return_value=notifier))
            stack.enter_context(patch('autograb.order_cli.EventLog'))
            for name in ('arm','arm_smoke','issue_permit','assert_permit'):
                denied=stack.enter_context(patch.object(RealOrderSmokeGuard,name,side_effect=AssertionError('read acquired '+name)))
                stack.callback(denied.assert_not_called)
            for code,outcome in ((0,'PRECHECK_READY'),(2,'LOGIN_REQUIRED')):
                with redirect_stdout(StringIO()) as output:
                    self.assertEqual(await run_order(self.args('order-precheck',identity),self.config),code)
                report=json.loads(output.getvalue())
                self.assertEqual(report['observation']['outcome'],outcome)
                self.assertFalse(report['REAL_ORDER_SMOKE_TEST_ARMED'])
                self.assertEqual(report['LIVE'],'OFF')
        self.assertEqual(provider.precheck_order.await_count,2)
        provider.submit_order.assert_not_called();provider.reconcile_order.assert_not_called()
        self.assertEqual(notifier.mock_calls,[])
        with Store(path) as store:
            saved=IntentStore(store).get(identity)
            self.assertIsNone(saved['submission_nonce']);self.assertIsNone(saved['submit_started_at'])
            self.assertIsNone(saved['order_id']);self.assertIsNone(saved['invoice_id'])
            self.assertEqual(store.connection.execute('SELECT count(*) FROM notifications').fetchone()[0],0)
