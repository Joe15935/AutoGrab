"""Explicit shared L6/L7 controls. Default operations are local/read-only.

Normal monitoring ARM never authorizes order-smoke. The separate foreground
smoke permit defaults false, expires in <=60s and is consumed once. Historical
simulated intents cannot be upgraded into real opportunities by this command.
"""
import json
from datetime import datetime, timedelta, timezone

from autograb.core.errors import AutoGrabError
from autograb.core.events import EventLog
from autograb.core.live import Preflight, SMTPProof, RealOrderSmokeGuard, signal_present
from autograb.core.lock import ProcessLock
from autograb.core.models import Product
from autograb.core.purchase import PurchaseRunner
from autograb.core.purchase_timing import PurchaseTiming
from autograb.notifications.email import EmailNotifier
from autograb.notifications.setup import load_setup
from autograb.providers.edge_order import EdgeOrderProvider
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore


def register_commands(commands):
    commands.add_parser('provider-capabilities').set_defaults(order_core=True)
    commands.add_parser('order-status').set_defaults(order_core=True)
    for name in ('order-precheck', 'order-smoke', 'order-reconcile'):
        p = commands.add_parser(name)
        p.set_defaults(order_core=True)
        p.add_argument('--intent-id', required=True)
        p.add_argument('--tab-id', type=int, required=True, help='Exact ordinary Edge task tab; never guessed')
        p.add_argument('--period', choices=['monthly','quarterly','semiannually','annually','biennially','triennially'])
        if name == 'order-smoke':
            p.add_argument('--mode', choices=['DRY_RUN','LIVE'], default='DRY_RUN')
            p.add_argument('--real-order-smoke-test-armed', action='store_true', default=False)
            p.add_argument('--test-email', action='store_true', help='Explicitly send a preflight SMTP test')
            p.add_argument('--seconds', type=int, default=60)


async def run_order(args, config):
    from autograb.core.capabilities import capabilities
    if args.command == 'provider-capabilities':
        print(json.dumps(capabilities(), indent=2)); return 0
    if args.command == 'order-smoke' and (args.mode != 'LIVE' or args.real_order_smoke_test_armed is not True):
        print(json.dumps({'status':'REAL_ORDER_SMOKE_TEST_NOT_ARMED', 'REAL_ORDER_SMOKE_TEST_ARMED':False, 'LIVE':'OFF','automatic_payment':'DISABLED'})); return 2
    with ProcessLock(config.root/'data/autograb.lock'), Store(config.root/'data/autograb.sqlite3') as store:
        intents = IntentStore(store)
        intents.recover_interrupted()
        if args.command == 'order-status':
            rows = store.connection.execute('SELECT provider,state,count(*) FROM purchase_intents GROUP BY provider,state').fetchall()
            print(json.dumps({'states':[{'provider':r[0],'state':r[1],'count':r[2]} for r in rows],
                'REAL_ORDER_SMOKE_TEST_ARMED':False,'LIVE':'OFF','ARM':'OFF','automatic_payment':'DISABLED'})); return 0
        intent = intents.get(args.intent_id)
        if intent is None:
            raise AutoGrabError('INTENT_NOT_FOUND')
        if args.command == 'order-smoke' and intent['origin'] != 'REAL':
            raise AutoGrabError('SIMULATED_EVENT_CANNOT_ORDER')
        if args.command == 'order-reconcile' and intent['submit_started_at'] is None:
            raise AutoGrabError('ORDER_NOT_SUBMITTED')
        provider = EdgeOrderProvider(store,intent['provider'],args.tab_id,period=args.period)
        notifier = EmailNotifier(load_setup(config.root,base=config.smtp))
        guard = RealOrderSmokeGuard(config.root/'data', provider=intent['provider'],mode=getattr(args,'mode','DRY_RUN'))
        log = EventLog(config.root/'logs/events.jsonl',provider=intent['provider'],mode=getattr(args,'mode','DRY_RUN'))
        proof = None
        async def preflight():
            current = intents._required(args.intent_id)
            check = current.get('order_precheck') or {}
            connected = provider.broker.status()['connected']
            checks = {'database':'PASS' if store.connection.execute('PRAGMA quick_check').fetchone()[0]=='ok' else 'FAIL',
                'browser':'PASS' if connected else 'FAIL',
                'site':'PASS' if connected and check.get('challenge')=='NONE' else 'NOT_READY',
                'session':'PASS' if check.get('login')=='VALID' else 'LOGIN_REQUIRED',
                'baseline':'PASS' if store.connection.execute('SELECT 1 FROM products WHERE provider=? LIMIT 1',(intent['provider'],)).fetchone() else 'NOT_READY',
                'email':'PASS' if proof else 'NOT_READY',
                'boundary':'PASS' if check.get('no_charge_verified') is True else 'NOT_READY'}
            return Preflight(checks,smtp_proof=proof)
        runner = PurchaseRunner(store,provider,notifier,log,guard,preflight)
        if args.command == 'order-precheck':
            observed = await runner.precheck_checkout(args.intent_id)
            print(json.dumps({'status':'ORDER_PRECHECK','observation':observed,'REAL_ORDER_SMOKE_TEST_ARMED':False,'LIVE':'OFF'}))
            return 0 if observed.get('outcome')=='PRECHECK_READY' else 2
        if args.command == 'order-reconcile':
            event = store.get_event(intent['event_id'])
            result = await runner._reconcile_one(event,Product.from_dict(event['product']),intent,PurchaseTiming(),[],recovery=True)
        else:
            if not 1 <= args.seconds <= 60:
                raise AutoGrabError('SMOKE_EXPIRY_INVALID')
            if signal_present(config.root/'data','disarm') or signal_present(config.root/'data','stop_monitoring'):
                raise AutoGrabError('KILL_SWITCH_ACTIVE')
            check = await runner.precheck_checkout(args.intent_id)
            if check.get('outcome') != 'PRECHECK_READY':
                print(json.dumps({'status':'ORDER_PRECHECK_BLOCKED','observation':check,'REAL_ORDER_SMOKE_TEST_ARMED':False}));return 2
            if args.test_email:
                sent = await notifier.send_live_test()
                if sent.status=='SMTP_ACCEPTED':
                    proof = SMTPProof('SMTP_ACCEPTED','REAL_SMTP',datetime.now(timezone.utc))
            guard.arm_smoke(await preflight(),args.intent_id,confirmed=True,duration=timedelta(seconds=args.seconds))
            try:
                result = await runner.submit_checkout(args.intent_id,guard)
            finally:
                guard.disarm()
        print(json.dumps({**result,'REAL_ORDER_SMOKE_TEST_ARMED':False,'automatic_payment':'DISABLED'},ensure_ascii=False))
        return 0 if result['status'] in {'PAYMENT_READY','WAITING_FOR_USER'} else 2
