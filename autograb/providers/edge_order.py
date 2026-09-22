"""Thin BWH/DMIT adapter over existing normal Edge Native Messaging.

Only fixed commands are sent. No HTTP account client, alternate browser, cookie
reader or payment operation exists. A timeout never resends the command.
"""
import asyncio
from contextlib import nullcontext
from pathlib import Path

from autograb.core.errors import AutoGrabError
from autograb.core.lock import ProcessLock, submission_lease_path
from autograb.edge.broker import EdgeBroker


class EdgeOrderProvider:
    simulation_only = False

    def __init__(self, store, provider, tab_id, *, period=None, timeout=20):
        if provider not in {'bandwagon', 'dmit'}:
            raise AutoGrabError('ORDER_PROVIDER_UNSUPPORTED')
        if type(tab_id) is not int or not 0 <= tab_id < 2**31:
            raise AutoGrabError('TAB_IDENTITY_REQUIRED')
        self.store, self.provider_name, self.tab_id = store, provider, tab_id
        self.period, self.timeout = period, timeout
        self.broker = EdgeBroker(store)

    def product_payload(self, intent, product):
        if (product.provider, product.product_id) != (self.provider_name, intent['product_id']):
            raise AutoGrabError('PRODUCT_MISMATCH')
        period = self.period or (intent.get('order_precheck') or {}).get('billing')
        prices = [p for p in product.prices if p.get('available') is True and (not period or p.get('period') == period)]
        if len(prices) != 1:
            raise AutoGrabError('EXACT_BILLING_REQUIRED')
        return {'name': product.name, 'url': product.product_url, **{k: prices[0][k] for k in ('period', 'cents', 'currency')}}

    async def _exchange(self, kind, intent, product, **extra):
        mode = 'REAL_ORDER_SMOKE_TEST' if kind == 'SUBMIT_ORDER' else 'DRY_RUN'
        # This kernel lease dies with the foreground process. The native host
        # must not dispatch a persisted write after its authorizer has exited.
        lease = (ProcessLock(submission_lease_path(Path(self.store.path).parent, extra['permit']['nonce']))
                 if kind == 'SUBMIT_ORDER' else nullcontext())
        with lease:
            message = self.broker.enqueue_order(kind, intent['intent_id'],
                {'product': self.product_payload(intent, product), 'mode': mode, 'tab_id': self.tab_id, **extra})
            try:
                deadline = asyncio.get_running_loop().time() + self.timeout
                while asyncio.get_running_loop().time() < deadline:
                    result = self.broker.order_result(message['command_id'])
                    if result['observation'] is not None:
                        return result['observation']
                    if result['status'] == 'HALTED' or not self.broker.status()['connected']:
                        raise AutoGrabError('EDGE_DISCONNECTED')
                    await asyncio.sleep(0.1)
                raise AutoGrabError('ORDER_RESPONSE_TIMEOUT')
            finally:
                # Includes cancellation, disconnect and timeout. A dispatched
                # action stays uncertain; HALTED is only local transport state.
                with self.store._transaction():
                    self.store.connection.execute("UPDATE edge_commands SET status='HALTED' WHERE command_id=? AND status IN ('QUEUED','DISPATCHED')", (message['command_id'],))

    async def precheck_order(self, intent, product):
        return await self._exchange('ORDER_PRECHECK', intent, product)

    def _receipt(self, observation, intent):
        status = observation['outcome']
        if status in {'LOGIN_REQUIRED', 'HUMAN_ACTION_REQUIRED'}:
            return {'status': status}
        if status == 'NO_ORDER_FOUND':
            return {'status': 'NO_ORDER_FOUND', 'absence_evidence': observation}
        if status not in {'ORDER_FOUND', 'INVOICE_FOUND', 'PAYMENT_READY'}:
            return {'status': 'UNKNOWN'}
        receipt = {'status': 'FOUND', 'order_id': observation['order_id']}
        if 'invoice_id' in observation:
            receipt['invoice_id'] = observation['invoice_id']
        if 'official_url' in observation:
            receipt['payment_url'] = observation['official_url']
        if status == 'PAYMENT_READY':
            receipt['verification'] = {
                'provider': self.provider_name,
                'merchant': {'bandwagon': 'bandwagonhost.com', 'dmit': 'www.dmit.io'}[self.provider_name],
                'product_id': intent['product_id'], 'order_id': receipt['order_id'],
                'invoice_id': receipt['invoice_id'], 'payment_url': receipt['payment_url'],
                'merchant_verified': True, 'product_verified': observation['product_verified'],
                'amount_present': True, 'payment_page_verified': True,
                'unpaid_verified': observation['unpaid'], 'invoice_status': 'UNPAID',
                'source': 'REAL_SITE', 'amount_cents': observation['amount_cents'],
                'currency': observation['currency'], 'billing': observation['billing'],
                'observed_at': observation['observed_at'], 'login_required': True,
            }
        return receipt

    async def submit_order(self, intent, product, permit):
        observation = await self._exchange('SUBMIT_ORDER', intent, product, permit=permit)
        return self._receipt(observation, intent)

    async def reconcile_order(self, intent, product):
        observation = await self._exchange('RECONCILE_ORDER', intent, product,
            submission_nonce=intent['submission_nonce'],
            submitted_at=intent['submit_started_at'] if intent['submission_nonce'] else None,
            order_id=intent['order_id'], invoice_id=intent['invoice_id'])
        return self._receipt(observation, intent)

    async def reconcile_intent(self, intent, product):
        return await self.reconcile_order(intent, product)
