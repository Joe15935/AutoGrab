"""Phase 2R public entry points. Authenticated execution belongs to normal Edge."""
from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

from autograb.core.lock import ProcessLock
from autograb.core.live import signal_disarm, signal_stop_monitoring
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore
from autograb.edge_install import install, installation_status, open_edge
from autograb.edge.protocol import CURRENCIES, PERIODS, PROVIDERS


def register_commands(commands):
    for name in ("edge-install", "edge-status", "edge-open"):
        commands.add_parser(name).set_defaults(edge=True)
    command = commands.add_parser("edge-dry-run")
    command.add_argument("--provider", choices=sorted(PROVIDERS), default="bandwagon")
    command.add_argument("--product-id", required=True)
    command.add_argument("--wait-seconds", type=int, default=120)
    command.set_defaults(edge=True)
    for name in ("edge-resume", "edge-cancel"):
        command = commands.add_parser(name)
        command.add_argument("--intent-id", required=True)
        command.add_argument("--wait-seconds", type=int, default=120)
        command.set_defaults(edge=True)


def handles(args) -> bool:
    return bool(getattr(args, "edge", False) or args.command in {
        "open-session", "configure", "arm", "reconcile", "disarm", "stop", "stop-all"
    } or (args.command == "preflight" and not getattr(args, "offline", False)))


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def product_payload(product):
    if product.provider != "bandwagon":
        prices = [price for price in product.prices if price.get("available", True) is not False
                  and type(price.get("cents")) is int and 0 < price["cents"] < 10**12
                  and price.get("currency") in CURRENCIES
                  and str(price.get("period", "")).lower() in PERIODS - {"unknown"}]
        price = prices[0] if prices else {"period": "unknown", "cents": None, "currency": None}
        return {"mode": "DRY_RUN", "product": {"name": product.name, "url": product.product_url,
                "period": str(price["period"]).lower(), "cents": price["cents"], "currency": price["currency"]}}
    prices = [price for price in product.prices if price.get("available", True)
              and str(price.get("period", "")).lower() in {"annually", "biennially"}]
    prices.sort(key=lambda price: str(price["period"]).lower() != "annually")
    if not prices or product.availability != "AVAILABLE":
        raise ValueError("AVAILABLE_PRODUCT_AND_PRICE_REQUIRED")
    price = prices[0]
    return {"mode": "DRY_RUN", "product": {"name": product.name, "url": product.product_url,
            "period": str(price["period"]).lower(), "cents": price["cents"], "currency": price["currency"]}}


async def wait_intent(broker, intents, intent_id, seconds):
    deadline = time.monotonic() + max(0, min(seconds, 600))
    last = None
    while True:
        intent = intents.get(intent_id)
        state = intent.get("edge_state") or intent["state"]
        if state != last:
            output({"intent_id": intent_id, "state": intent["state"], "edge_state": state,
                    "error_code": intent.get("edge_error_code"),
                    "live": "OFF", "orders_created": 0, "payments": 0})
            last = state
        if intent["state"] == "CHECKOUT_READY":
            return 0
        if state == "OPENED":
            output({"status": "READ_ONLY_PAGE_OPENED", "checkout_verified": False, "live": "OFF"})
            return 2
        if state in {"WAITING_FOR_HUMAN", "LOGIN_REQUIRED", "FAILED", "SITE_CHANGED", "SOLD_OUT", "DISCONNECTED", "CANCELLED", "PAUSED"}:
            return 2
        if time.monotonic() >= deadline:
            output({"status": "PENDING", "instruction": "Use edge-status; keep this same intent. Do not start another attempt."})
            return 2
        await asyncio.sleep(1)


async def run_edge(args, config):
    from autograb.edge.broker import EdgeBroker
    command = args.command
    if command == "configure":
        print("1. 配置／测试邮件\n2. 打开日常 Edge 登录页\n3. 安装 Edge Companion\n4. 查看扩展状态\n0. 退出")
        choice = input("请选择：").strip()
        if choice == "1":
            from autograb.phase2_cli import run_phase2
            from argparse import Namespace
            return await run_phase2(Namespace(command="configure-email"), config)
        command = {"2": "edge-open", "3": "edge-install", "4": "edge-status"}.get(choice, "exit")
    if command == "exit":
        return 0
    if command in {"edge-open", "open-session"}:
        open_edge("https://bandwagonhost.com/clientarea.php")
        output({"browser": "NORMAL_MICROSOFT_EDGE", "session": "AWAITING_VERIFICATION", "live": "OFF"})
        return 0
    if command == "edge-install":
        output(install(config.root))
        print("请在 Edge 扩展页面开启开发人员模式，点击‘加载解压缩的扩展’，选择刚打开的 edge-extension 文件夹。\n安装后打开 AutoGrab 扩展弹窗并连接；正常登录和真人验证仍由本人完成。")
        return 0
    with Store(config.root / "data/autograb.sqlite3") as store:
        intents = IntentStore(store)
        broker = EdgeBroker(store)
        if command == "edge-status":
            output({**installation_status(config.root), **broker.status(), "live": "OFF"})
            return 0
        if command in {"disarm", "stop", "stop-all"}:
            if command != "disarm":
                signal_stop_monitoring(config.root / "data")
            if command != "stop":
                signal_disarm(config.root / "data")
            broker.enqueue("DISARM")
            output({"status": "STOP_REQUESTED", "live": "OFF", "existing_orders": "PRESERVED"})
            return 0
        if command in {"arm", "preflight", "reconcile"}:
            output({"status": "NOT_READY", "edge": broker.status(), "live": "OFF",
                    "reason": "EDGE_ORDER_ADAPTER_NOT_VERIFIED", "playwright_authenticated_checkout": "DISABLED",
                    "instruction": "Complete edge-dry-run first. No real order or automatic reconciliation is enabled."})
            return 2
        if command == "edge-cancel":
            intent = intents.get(args.intent_id)
            if not intent:
                raise ValueError("UNKNOWN_INTENT")
            broker.enqueue("CANCEL_INTENT", intent["intent_id"], intent["product_id"])
            output({"status": "LOCAL_CANCEL_REQUESTED", "intent_id": intent["intent_id"], "server_cart": "PRESERVED", "live": "OFF"})
            return 0
        with ProcessLock(config.root / "data/autograb.lock"):
            products = {(p.provider, p.product_id): p for p in store.list_products()}
            if command == "edge-resume":
                intent = intents.get(args.intent_id)
                if not intent or (intent["provider"], intent["product_id"]) not in products:
                    raise ValueError("UNKNOWN_INTENT_OR_PRODUCT")
                payload = product_payload(products[(intent["provider"], intent["product_id"])])
                broker.enqueue("RESUME_INTENT", intent["intent_id"], intent["product_id"], payload)
            else:
                product = products.get((getattr(args, "provider", "bandwagon"), args.product_id))
                if not product:
                    raise ValueError("PRODUCT_NOT_IN_BASELINE")
                payload = product_payload(product)
                from autograb.edge.protocol import make_message
                # New provider adapters currently verify public pages only.
                read_only = product.provider != "bandwagon"
                kind = "OPEN_PRODUCT" if read_only else "START_DRY_RUN"
                make_message(kind, intent_id=str(uuid4()), product_id=product.product_id,
                             provider=product.provider, payload=payload)
                # This is an explicit real-page dry test, not a synthetic stock opportunity.
                current_id = broker.status().get("current_intent")
                active = intents.active_for_product(product.product_id, product.provider)
                if not active and current_id and not broker.can_start_provider(product.provider):
                    active = intents.get(current_id)
                if active:
                    output({"status": "EXISTING_INTENT", "intent_id": active["intent_id"],
                            "instruction": "Use edge-status / edge-resume for the same intent; no duplicate attempt created."})
                    return 2
                if not broker.status().get("connected"):
                    open_edge(product.product_url)
                    output({"status": "EDGE_CONNECTION_REQUIRED", "instruction": "Connect AutoGrab in Edge, then run this command again. No intent or cart action created."})
                    return 2
                event = store.create_event(product, "PRODUCT_CHANGED")
                store.update_event(event["id"], "EDGE_DRY_RUN", {"mode": "DRY_RUN", "source": "EXPLICIT_USER_TEST"})
                intent = intents.create(event, product)
                broker.enqueue(kind, intent["intent_id"], product.product_id, payload)
                if read_only:
                    output({"status": "READ_ONLY_DIAGNOSTIC", "provider": product.provider,
                            "reason": "ADAPTER_READ_ONLY", "checkout_verified": False})
            return await wait_intent(broker, intents, intent["intent_id"], args.wait_seconds)
