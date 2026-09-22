"""Public multi-provider monitoring on the existing database and Edge broker."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from autograb.core.errors import AutoGrabError
from autograb.core.events import EventLog
from autograb.core.live import signal_present
from autograb.core.lock import ProcessLock
from autograb.providers.registry import NAMES, create_provider
from autograb.storage.database import Store
from autograb.notifications.email import EmailNotifier
from autograb.notifications.setup import load_setup


async def process_opportunity(store, provider, event, notifier, *, prepare_checkout=False, data_dir=None):
    """Claim once; recheck official stock before any checkout preparation."""
    event = store.get_event(event["id"])
    if event is None or provider.provider_name != event["provider"]:
        raise AutoGrabError("PROVIDER_IDENTITY_MISMATCH")
    details = event.get("details", {})
    if not details.get("opportunities"):
        store.update_event(event["id"], "RECORDED_ONLY", {"reason": "REGULAR_OR_METADATA_ONLY"})
        return "REGULAR"
    if not store.claim_event(event["id"]):
        return "ALREADY_CONSUMED"
    from autograb.core.models import Product
    observed = Product.from_dict(event["product"])
    status = "OPPORTUNITY_RECORDED"
    if details.get("execution_candidate") and prepare_checkout:
        def check_controls():
            directory = data_dir if data_dir is not None else Path(store.path).parent if store.path != ":memory:" else None
            if directory is not None:
                for signal in ("stop_monitoring", "disarm"):
                    if signal_present(directory, signal):
                        raise AutoGrabError("MONITORING_STOPPED" if signal == "stop_monitoring" else "DISARMED")
        intent = None
        try:
            check_controls()
            # These adapters can observe public pages only; never reserve an
            # executable intent which the extension must immediately reject.
            if observed.provider != "bandwagon":
                raise AutoGrabError("ADAPTER_READ_ONLY")
            current = await provider.check_product(observed.product_id)
            check_controls()
            if current.provider != observed.provider or current.product_id != observed.product_id:
                raise AutoGrabError("OFFICIAL_IDENTITY_CHANGED")
            if (current.name, current.product_url, current.prices) != (observed.name, observed.product_url, observed.prices):
                raise AutoGrabError("OFFICIAL_PRODUCT_CHANGED")
            if current.availability != "AVAILABLE":
                status = "OFFICIAL_STOCK_UNCONFIRMED"
            else:
                # Shared existing transport and intent ledger; never a payment permit.
                from autograb.edge_cli import product_payload
                from autograb.edge.broker import EdgeBroker
                from autograb.storage.intents import IntentStore
                broker, intents = EdgeBroker(store), IntentStore(store)
                if not broker.status()["connected"]:
                    status = "EDGE_CONNECTION_REQUIRED"
                elif intents.active_for_product(current.product_id, current.provider):
                    status = "EXISTING_INTENT"
                elif not broker.can_start_provider(current.provider):
                    status = "EDGE_EXECUTOR_BUSY"
                else:
                    # Validate before reserving the one-shot intent.
                    from autograb.edge.protocol import make_message
                    payload = product_payload(current)
                    make_message("START_DRY_RUN", provider=current.provider,
                                 intent_id=str(uuid4()), product_id=current.product_id, payload=payload)
                    check_controls()
                    intent = intents.create(event, current)
                    check_controls()
                    broker.enqueue("START_DRY_RUN", intent["intent_id"], current.product_id, payload)
                    status = "EDGE_DRY_RUN_QUEUED"
        except (AutoGrabError, ValueError) as error:
            # Only release a newly-created intent proven never to have entered
            # the transport. Uncertain/dispatched commands retain their lock.
            if intent is not None:
                saved = intents.get(intent["intent_id"])
                queued = store.connection.execute("SELECT 1 FROM edge_commands WHERE intent_id=?", (intent["intent_id"],)).fetchone()
                if saved and saved["state"] == "INTENT_CREATED" and saved.get("edge_state") is None and queued is None:
                    intents.abort_before_submit(intent["intent_id"])
            # No replay on uncertainty. Fixed error identifiers only.
            code = getattr(error, "code", "EDGE_REVIEW_REQUIRED")
            status = code if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum() else "EDGE_REVIEW_REQUIRED"
    # Persist notification dispatch before SMTP; an uncertain send is not retried.
    store.update_event(event["id"], "NOTIFYING", {"checkout_status": status})
    result = await notifier.send_event(observed, event, {}, {"status": status})
    store.record_notification(event["id"], result.status, result.error_code or "")
    store.update_event(event["id"], "OPPORTUNITY_NOTIFIED" if result.status == "SMTP_ACCEPTED" else "OPPORTUNITY_RECORDED",
                       {"notification": result.status, "checkout_status": status})
    return status


async def run_multi(args, config):
    selection = getattr(args, "provider", "all")
    names = [name for name in NAMES if selection in {"all", name}
             and config.providers.get(name, {}).get("enabled", True)]
    if not names:
        raise ValueError("No providers enabled")
    logs = {name: EventLog(config.root / "logs/events.jsonl", provider=name) for name in names}
    notifier = EmailNotifier(load_setup(config.root, base=config.smtp))
    blocked = set()
    paused_report = {}
    with ProcessLock(config.root / "data/autograb.lock"), Store(config.root / "data/autograb.sqlite3") as store:
        providers = {name: create_provider(name, config, logs[name], store=store) for name in names}
        store.recover_interrupted()
        run_id = store.start_run("multi_" + args.command)
        try:
            while not signal_present(config.root / "data", "stop_monitoring"):
                active = [name for name in names if name not in blocked]
                results = await asyncio.gather(*(providers[n].discover_products() for n in active), return_exceptions=True)
                if signal_present(config.root / "data", "stop_monitoring"):
                    break
                report = dict(paused_report)
                for name, result in zip(active, results):
                    if isinstance(result, Exception):
                        code = result.code if isinstance(result, AutoGrabError) else "DATA_SOURCE_UNAVAILABLE"
                        report[name] = {"status": "USER_ACTION_REQUIRED" if code in {"HUMAN_CHALLENGE_REQUIRED", "LOGIN_REQUIRED", "APPLE_TARGETS_NOT_CONFIGURED"} else "BLOCKED", "reason": code}
                        rate_status = getattr(providers[name], "rate_status", None)
                        if rate_status:
                            report[name]["rate_budget"] = rate_status
                            if code in {"RATE_LIMIT_WAIT", "RATE_LIMITED", "CATALOG_INCOMPLETE", "HTTP_BLOCKED"}:
                                report[name]["status"] = "WAITING"
                        logs[name].write("PROVIDER_PAUSED", code=code)
                        if (code in {"HUMAN_CHALLENGE_REQUIRED", "LOGIN_REQUIRED", "APPLE_TARGETS_NOT_CONFIGURED", "HTTP_403"}
                                or (code == "RATE_LIMITED" and not rate_status)):
                            blocked.add(name)
                            paused_report[name] = report[name]
                        continue
                    if args.command == "probe":
                        report[name] = {"status": "DISCOVERED", "count": len(result)}
                        if getattr(providers[name], "rate_status", None):
                            report[name]["rate_budget"] = providers[name].rate_status
                        continue
                    if name == "apple":
                        from autograb.providers.apple_catalog import merge_observations
                        result = merge_observations(result, store.list_products())
                    snapshot = store.ingest(result, source=getattr(providers[name], "source", None) or "OFFICIAL_PUBLIC_INVENTORY")
                    opportunities = 0
                    for event in snapshot["events"]:
                        if args.command == "monitor":
                            if signal_present(config.root / "data", "stop_monitoring"):
                                break
                            await process_opportunity(store, providers[name], event, notifier,
                                prepare_checkout=getattr(args, "prepare_checkout", False), data_dir=config.root / "data")
                            opportunities += bool(event.get("details", {}).get("opportunities"))
                        else:
                            store.update_event(event["id"], "RECORDED_ONLY", {"reason": "EXPLICIT_BASELINE_COMMAND"})
                    report[name] = {"status": "BASELINE" if snapshot["baseline_initialized"] else "MONITORED",
                        "known_count": snapshot["known_count"], "observed_count": len(result),
                        "opportunities": opportunities, "orders_created": 0, "payments": 0}
                    if getattr(providers[name], "rate_status", None):
                        report[name]["rate_budget"] = providers[name].rate_status
                print(json.dumps({"providers": report, "live": "OFF", "arm": "OFF"}, ensure_ascii=False), flush=True)
                if args.command != "monitor" or args.once or len(blocked) == len(names):
                    status = "PARTIAL" if any(x["status"] in {"BLOCKED", "USER_ACTION_REQUIRED", "WAITING"} for x in report.values()) else "COMPLETE"
                    store.finish_run(run_id, status, report)
                    return 2 if status == "PARTIAL" else 0
                from autograb.phase2_cli import monitoring_wait
                if not await monitoring_wait(config.root / "data", config.normal_interval):
                    break
            store.finish_run(run_id, "STOPPED")
            return 0
        except BaseException:
            store.finish_run(run_id, "INTERRUPTED")
            raise
