"""Implemented contracts and dated real-site evidence are separate facts."""
from copy import deepcopy

_CAPABILITIES = {
    'bandwagon': {'implemented_level': 'L7_EXPERIMENTAL', 'verified_level': 'L5', 'real_order_verified': False},
    'dmit': {'implemented_level': 'L7_EXPERIMENTAL', 'verified_level': 'L5_MANUAL', 'real_order_verified': False},
    'vmiss': {'implemented_level': 'L3_EXPERIMENTAL', 'verified_level': 'L0_PARTIAL', 'status': 'RATE_LIMITED'},
    'vps': {'implemented_level': 'L4', 'verified_level': 'L4', 'boundary': 'REAL_ORDER_BOUNDARY'},
    'apple': {'implemented_level': 'INVENTORY_READY_BAG_EXPERIMENTAL', 'verified_level': 'L3', 'bag': 'APPLE_BAG_BLOCKED', 'checkout': 'UNVERIFIED'},
}
LEVELS = {'L0': 'DISCOVERY', 'L1': 'BASELINE', 'L2': 'OPPORTUNITY', 'L3': 'EDGE OPEN',
          'L4': 'CART / PRE-ORDER BOUNDARY', 'L5': 'CHECKOUT', 'L6': 'REAL ORDER CREATED', 'L7': 'PAYMENT_READY'}


def capabilities():
    return {'levels': dict(LEVELS), 'providers': deepcopy(_CAPABILITIES),
            'evidence_date': '2026-09-22', 'LIVE': 'OFF', 'ARM': 'OFF',
            'REAL_ORDER_SMOKE_TEST_ARMED': False, 'automatic_payment': 'DISABLED'}
