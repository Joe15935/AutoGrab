"""Bounded additions to envelope v1 for normal-Edge unpaid-order work.

No selector, script, cookie, account form value or external gateway URL belongs
in this contract. Read commands cannot carry submission authority.
"""
from datetime import datetime, timezone

from .protocol import ProtocolError, timestamp, _uuid, validate

ORDER_COMMANDS = frozenset({'ORDER_PRECHECK', 'SUBMIT_ORDER', 'RECONCILE_ORDER'})
OUTCOMES = frozenset({'PRECHECK_READY', 'ORDER_FOUND', 'INVOICE_FOUND', 'PAYMENT_READY',
                     'NO_ORDER_FOUND', 'UNKNOWN', 'LOGIN_REQUIRED', 'HUMAN_ACTION_REQUIRED'})
PERIODS = frozenset({'monthly', 'quarterly', 'semiannually', 'annually', 'biennially', 'triennially'})
REQUIRED = frozenset({'tab_id', 'stage', 'code', 'login', 'challenge', 'outcome'})
OPTIONAL = frozenset({'precheck_id', 'order_id', 'invoice_id', 'amount_cents', 'currency',
    'official_url', 'unpaid', 'observed_at', 'submission_nonce', 'product_verified',
    'billing', 'no_charge_verified', 'scope_complete', 'time_window_verified', 'created_at'})


def _require(condition, code='ORDER_SCHEMA_INVALID'):
    if not condition:
        raise ProtocolError(code)


def _tab(value):
    return type(value) is int and 0 <= value < 2**31


def official_invoice_url(value, provider, invoice_id):
    merchant = {'bandwagon': 'bandwagonhost.com', 'dmit': 'www.dmit.io'}.get(provider)
    return merchant is not None and value == f'https://{merchant}/viewinvoice.php?id={invoice_id}'


def validate_order_command(message):
    kind, payload, provider = message['type'], message['payload'], message['provider']
    _require(provider in {'bandwagon', 'dmit'}, 'ORDER_PROVIDER_UNSUPPORTED')
    extra = {'permit'} if kind == 'SUBMIT_ORDER' else {'submission_nonce', 'submitted_at', 'order_id', 'invoice_id'} if kind == 'RECONCILE_ORDER' else set()
    _require(set(payload) == {'product', 'mode', 'tab_id'} | extra)
    _require(_tab(payload['tab_id']))
    _require(payload['mode'] == ('REAL_ORDER_SMOKE_TEST' if kind == 'SUBMIT_ORDER' else 'DRY_RUN'))
    # Reuse the old product schema instead of maintaining a second URL parser.
    validate({**message, 'type': 'OPEN_PRODUCT', 'payload': {'product': payload['product'], 'mode': 'DRY_RUN'}}, direction='command', fresh=False)
    product = payload['product']
    _require(type(product['cents']) is int and 0 < product['cents'] < 10**12 and product['currency'] == 'USD' and product['period'] in PERIODS)
    if kind == 'SUBMIT_ORDER':
        permit = payload['permit']
        _require(type(permit) is dict and set(permit) == {'nonce', 'issued_at', 'expires_at', 'precheck_id', 'real_order_smoke_test_armed'})
        _require(permit['real_order_smoke_test_armed'] is True and _uuid(permit['nonce']) and _uuid(permit['precheck_id']))
        issued, expires = timestamp(permit['issued_at']), timestamp(permit['expires_at'])
        _require(0 < (expires-issued).total_seconds() <= 60, 'ORDER_PERMIT_INVALID')
    elif kind == 'RECONCILE_ORDER':
        import re
        for key in ('order_id', 'invoice_id'):
            _require(payload[key] is None or isinstance(payload[key], str) and re.fullmatch(r'[1-9][0-9]{0,39}', payload[key]))
        _require(payload['invoice_id'] is None or payload['order_id'] is not None)
        nonce, submitted = payload['submission_nonce'], payload['submitted_at']
        _require((nonce is None) == (submitted is None))
        if nonce is not None:
            _require(_uuid(nonce)); timestamp(submitted)
    return message


def permit_fresh(permit):
    try:
        now = datetime.now(timezone.utc)
        return timestamp(permit['issued_at']) <= now < timestamp(permit['expires_at'])
    except (KeyError, TypeError, ValueError):
        return False


def validate_observation(payload, provider):
    import re
    _require(provider in {'bandwagon', 'dmit'}, 'ORDER_PROVIDER_UNSUPPORTED')
    _require(type(payload) is dict and REQUIRED <= payload.keys() and not payload.keys() - REQUIRED - OPTIONAL)
    _require(_tab(payload['tab_id']) and payload['outcome'] in OUTCOMES)
    for name in ('stage', 'code'):
        _require(isinstance(payload[name], str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', payload[name]))
    _require(payload['login'] in {'VALID', 'REQUIRED', 'UNKNOWN'} and payload['challenge'] in {'NONE', 'REQUIRED', 'UNKNOWN'})
    for name in ('precheck_id', 'submission_nonce'):
        if name in payload: _require(_uuid(payload[name]))
    for name in ('order_id', 'invoice_id'):
        if name in payload: _require(isinstance(payload[name], str) and re.fullmatch(r'[1-9][0-9]{0,39}', payload[name]))
    if 'amount_cents' in payload: _require(type(payload['amount_cents']) is int and 0 < payload['amount_cents'] < 10**12)
    if 'currency' in payload: _require(payload['currency'] == 'USD')
    if 'billing' in payload: _require(payload['billing'] in PERIODS)
    for name in ('unpaid', 'product_verified', 'no_charge_verified', 'scope_complete', 'time_window_verified'):
        if name in payload: _require(type(payload[name]) is bool)
    if 'observed_at' in payload: timestamp(payload['observed_at'])
    if 'created_at' in payload:
        _require('observed_at' in payload)
        _require(timestamp(payload['created_at']) <= timestamp(payload['observed_at']))
    if 'official_url' in payload:
        _require('invoice_id' in payload and official_invoice_url(payload['official_url'], provider, payload['invoice_id']), 'ORDER_INVOICE_URL_INVALID')
    outcome = payload['outcome']
    if outcome in {'PRECHECK_READY', 'ORDER_FOUND', 'INVOICE_FOUND', 'PAYMENT_READY', 'NO_ORDER_FOUND'}:
        _require(payload['login'] == 'VALID' and payload['challenge'] == 'NONE' and payload.get('product_verified') is True)
        _require({'observed_at', 'amount_cents', 'currency', 'billing'} <= payload.keys())
    if outcome == 'PRECHECK_READY':
        _require('precheck_id' in payload and payload.get('no_charge_verified') is True)
    if outcome in {'ORDER_FOUND', 'INVOICE_FOUND', 'PAYMENT_READY'}:
        _require('order_id' in payload)
    if outcome in {'INVOICE_FOUND', 'PAYMENT_READY'}:
        _require('invoice_id' in payload)
    if outcome == 'PAYMENT_READY':
        _require('official_url' in payload and payload.get('unpaid') is True)
    if outcome == 'NO_ORDER_FOUND':
        _require(payload.get('scope_complete') is True and payload.get('time_window_verified') is True and 'submission_nonce' in payload)
        _require(not {'order_id', 'invoice_id', 'official_url'} & payload.keys())
    return payload
