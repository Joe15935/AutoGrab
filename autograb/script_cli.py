"""One-shot public monitoring shared by console commands and QingLong wrappers."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import resource
import sys
import time

from autograb import STARTED_AT
from autograb.core.config import Config
from autograb.core.errors import AutoGrabError
from autograb.core.events import EventLog
from autograb.core.http_metrics import RequestMeter, current_meter
from autograb.core.live import signal_present
from autograb.core.lock import ProcessLock
from autograb.core.rate_budget import BudgetWait, ProviderRateBudget
from autograb.core.scheduled import BURST, NORMAL, launch_timestamp, wait_until
from autograb.core.snapshot import ProviderSnapshot, ScanState, query_projection, save_projection
from autograb.notifications.adapter import NotificationAdapter
from autograb.notifications.email import EmailNotifier
from autograb.notifications.setup import load_setup
from autograb.providers.registry import NAMES, create_provider
from autograb.storage.database import Store

MODES = ("QUERY", "MONITOR", "DRY_RUN")


def parser():
    p = argparse.ArgumentParser(description="AutoGrab Script Mode — one public check; LIVE OFF / ARM OFF")
    p.add_argument("provider", choices=[*NAMES, "all"])
    p.add_argument("--root", type=Path, default=Path(os.environ.get("AUTOGRAB_ROOT", Path.cwd())))
    p.add_argument("--mode", choices=MODES, default=os.environ.get("AUTOGRAB_MODE", "MONITOR").upper())
    p.add_argument("--json", action="store_true", help="Include quiet outcomes and measured HTTP/runtime counters")
    p.add_argument("--prepare-checkout", action="store_true", help="Local Mac, DRY_RUN only; after opportunity email")
    p.add_argument("--launch-at", help="Known launch time, ISO 8601 with timezone offset")
    p.add_argument("--pace", choices=["NORMAL", "BURST"], default="NORMAL")
    p.add_argument("--burst-seconds", type=int, default=120, help="Bounded window, 1..300 seconds")
    return p


def configured(args, *, qinglong=False):
    if args.mode not in MODES:
        raise ValueError("AUTOGRAB_MODE must be QUERY, MONITOR or DRY_RUN")
    if qinglong and (args.mode == "DRY_RUN" or args.prepare_checkout):
        raise AutoGrabError("QINGLONG_MONITOR_ONLY")
    if args.prepare_checkout and (args.mode != "DRY_RUN" or sys.platform != "darwin"):
        raise AutoGrabError("LOCAL_MAC_DRY_RUN_REQUIRED")
    if args.mode == "QUERY" and (args.launch_at or args.prepare_checkout or args.pace == "BURST"):
        raise ValueError("QUERY only reads saved observations")
    if not 1 <= args.burst_seconds <= 300 or (args.pace == "BURST" and not args.launch_at):
        raise ValueError("BURST requires a known --launch-at and a window of 1..300 seconds")
    config = Config.load(args.root.resolve())
    config.providers = copy.deepcopy(config.providers)
    for name in NAMES:
        flag = os.environ.get(f"AUTOGRAB_{name.upper()}_ENABLED")
        if flag is not None:
            if flag.lower() not in {"true", "false", "1", "0"}:
                raise ValueError("Provider enabled environment flag must be true/false or 1/0")
            config.providers.setdefault(name, {})["enabled"] = flag.lower() in {"true", "1"}
    # Daily script pickup does not refetch the full Apple catalogue. The existing
    # explicit apple-catalog-refresh command retains that independent capability.
    config.providers.setdefault("apple", {})["catalog_enabled"] = False
    return config


def rss_mb():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


async def check_once(name, config, store, args):
    started = time.perf_counter()
    meter = RequestMeter(deadline=getattr(args, "launch_deadline", None))
    token = current_meter.set(meter)
    budget = ProviderRateBudget(store)
    interval = (BURST if args.pace == "BURST" else NORMAL)[name]
    settings = config.providers.get(name, {})
    configured_interval = settings.get("interval_seconds", interval)
    if type(configured_interval) not in (int, float) or not 1 <= configured_interval <= 86400:
        raise ValueError("Invalid provider interval_seconds")
    interval = max(interval, configured_interval)
    log = EventLog(config.root / "logs/events.jsonl", provider=name, echo=False)
    provider = create_provider(name, config, log, store=store)
    ticket = None
    cursor = None
    if name == "vmiss":
        from autograb.providers.vmiss import VMISSProvider
        provider = VMISSProvider(log=log, settings=settings, budget=budget,
                                scan_state=ScanState(store, "vmiss:catalog"))
    if name == "apple":
        cursor = ScanState(store, "apple:" + str(provider.monitor.region) + ":pickup_cursor")
        provider.monitor._cursor = (cursor.load() or {}).get("next", 0)
    row = {"provider": name, "mode": args.mode, "pace": args.pace,
           "orders_created": 0, "payments": 0}
    try:
        if name in {"bandwagon", "dmit", "vps"}:
            ticket = budget.claim(name, "global", "catalog", interval_seconds=interval, lease_seconds=1200)
        products = await provider.discover_products()
        if ticket and not budget.success(ticket):
            raise AutoGrabError("RATE_PROBE_EXPIRED")
        if signal_present(config.root / "data", "stop_monitoring"):
            raise AutoGrabError("MONITORING_STOPPED")
        if name == "apple":
            if cursor:
                cursor.save({"next": provider.monitor._cursor})
            if provider.status != "OBSERVED":
                raise AutoGrabError(provider.status)
            from autograb.providers.apple_catalog import merge_observations
            products = merge_observations(products, store.list_products())
        snapshot = ProviderSnapshot.observed(name, products)
        result = snapshot.ingest(store, source=getattr(provider, "source", None) or "OFFICIAL_PUBLIC_INVENTORY")
        notifier = NotificationAdapter(EmailNotifier(load_setup(config.root, base=config.smtp)))
        opportunities, failures, dispatches, edge_results = 0, [], [], []
        from autograb.multi_cli import process_opportunity
        for event in result["events"]:
            if signal_present(config.root / "data", "stop_monitoring"):
                break
            outcome = await process_opportunity(store, provider, event, notifier,
                prepare_checkout=args.prepare_checkout, data_dir=config.root / "data", notify_first=True)
            opportunities += bool(event.get("details", {}).get("opportunities"))
            if args.prepare_checkout:
                edge_results.append(outcome)
            if ticket and outcome in {"RATE_LIMITED", "HUMAN_CHALLENGE_REQUIRED", "HTTP_403"}:
                budget.failure(ticket, outcome, retry_after=getattr(provider, "last_retry_after", None), limited=True)
            if outcome in {"NOT_CONFIGURED", "NOTIFICATION_FAILED"}:
                failures.append(outcome)
            if outcome == "EDGE_DRY_RUN_QUEUED":
                dispatches.append(outcome)
        row.update(status="NOTIFICATION_FAILED" if failures else "BASELINE" if result["baseline_initialized"] else "MONITORED",
                   observed_at=snapshot.timestamp, observed_count=len(products), known_count=result["known_count"],
                   opportunities=opportunities, notifications_failed=len(failures), edge_queued=len(dispatches), edge_results=edge_results)
    except AutoGrabError as error:
        if ticket and error.code != "RATE_PROBE_EXPIRED":
            budget.failure(ticket, error.code, retry_after=getattr(error, "retry_after", None) or getattr(provider, "last_retry_after", None),
                limited=error.code in {"RATE_LIMITED", "HTTP_403", "HUMAN_CHALLENGE_REQUIRED", "HTTP_BLOCKED"})
        row.update(status="WAITING" if error.code in {"RATE_LIMIT_WAIT", "CATALOG_INCOMPLETE", "LAUNCH_WINDOW_ENDED"} else "BLOCKED", reason=error.code)
    except Exception:
        if ticket:
            budget.failure(ticket, "DATA_SOURCE_UNAVAILABLE")
        row.update(status="BLOCKED", reason="DATA_SOURCE_UNAVAILABLE")
    finally:
        current_meter.reset(token)
    region, endpoint = (provider.monitor.region, "pickup") if name == "apple" else ("global", "catalog")
    if isinstance(region, str) and region:
        row["rate_budget"] = budget.status(name, region, endpoint)
    row.update(request_count=meter.count, runtime_ms=round((time.perf_counter() - started) * 1000, 2), peak_rss_mb=rss_mb())
    save_projection(config.root, store, name, row)
    return row


async def execute(args, config, *, qinglong=False):
    names = [n for n in NAMES if args.provider in {n, "all"} and config.providers.get(n, {}).get("enabled", True)]
    startup_ms = round((time.perf_counter() - STARTED_AT) * 1000, 2)
    if args.mode == "QUERY":
        result = {"mode": "QUERY", "fresh": False, "request_count": 0,
                  "providers": [query_projection(config.root, n) for n in names], "live": "OFF", "arm": "OFF"}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    launch = launch_timestamp(args.launch_at) if args.launch_at else None
    if launch is not None and time.time() - launch > 60:
        raise AutoGrabError("LAUNCH_TIME_EXPIRED")
    deadline = (launch + args.burst_seconds) if args.pace == "BURST" else None
    if deadline is not None and time.time() >= deadline:
        raise AutoGrabError("LAUNCH_WINDOW_ENDED")
    args.launch_deadline = deadline
    notifier = EmailNotifier(load_setup(config.root, base=config.smtp))
    if launch is not None and not notifier.configured:
        raise AutoGrabError("EMAIL_CONFIGURATION_REQUIRED")
    if "apple" in names:
        from autograb.providers.apple import AppleInventoryMonitor
        status = AppleInventoryMonitor(config.providers.get("apple", {})).status
        if launch is not None and status != "CONFIGURED":
            raise AutoGrabError(status)
    config.prepare()
    async def warm():
        if args.prepare_checkout:
            from autograb.edge.broker import EdgeBroker
            with Store(config.root / "data/autograb.sqlite3") as store:
                if not EdgeBroker(store).status()["connected"]:
                    raise AutoGrabError("EDGE_CONNECTION_REQUIRED")
        # No merchant request or test email consumes the launch budget.
        if not EmailNotifier(load_setup(config.root, base=config.smtp)).configured:
            raise AutoGrabError("EMAIL_CONFIGURATION_REQUIRED")
    if launch is not None:
        await wait_until(launch, data_dir=config.root / "data", warm=warm)
    rows = []
    while True:
        if signal_present(config.root / "data", "stop_monitoring"):
            raise AutoGrabError("MONITORING_STOPPED")
        try:
            with ProcessLock(config.root / "data/autograb.lock"), Store(config.root / "data/autograb.sqlite3") as store:
                run_id = store.start_run("script_" + args.mode.lower())
                batch = []
                try:
                    for name in names:
                        batch.append(await check_once(name, config, store, args))
                    store.finish_run(run_id, "PARTIAL" if any(r["status"] in {"BLOCKED", "NOTIFICATION_FAILED"} for r in batch) else "COMPLETE", {"providers": batch})
                except BaseException:
                    store.finish_run(run_id, "INTERRUPTED")
                    raise
        except AutoGrabError as error:
            if error.code != "ALREADY_RUNNING":
                raise
            batch = [{"provider": n, "status": "WAITING", "reason": "ALREADY_RUNNING", "request_count": 0} for n in names]
        rows.extend(batch)
        # A block ends that provider's window, not another provider's work.
        names = [n for n in names if not any(r["provider"] == n and r["status"] == "BLOCKED" for r in batch)]
        if deadline is None or not names:
            break
        wake = min((r.get("rate_budget", {}).get("wait_seconds", BURST[r["provider"]]) for r in batch if r["provider"] in names), default=60)
        target = time.time() + max(1, wake)
        if target >= deadline:
            break
        await wait_until(target, data_dir=config.root / "data")
    result = {"mode": args.mode, "qinglong": qinglong, "providers": rows, "startup_ms": startup_ms,
              "request_count": sum(r["request_count"] for r in rows), "peak_rss_mb": rss_mb(), "live": "OFF", "arm": "OFF"}
    if args.json or any(r["status"] in {"BLOCKED", "NOTIFICATION_FAILED"} or r.get("opportunities") for r in rows):
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 2 if any(r["status"] in {"BLOCKED", "NOTIFICATION_FAILED"} for r in rows) else 0


def run_provider(provider, *, root=None, mode=None, qinglong=False, argv=None):
    args = parser().parse_args([provider, *(argv or [])])
    if root is not None:
        args.root = Path(root)
    if mode is not None:
        args.mode = mode
    return _run(args, qinglong=qinglong)


def _run(args, *, qinglong=False):
    try:
        config = configured(args, qinglong=qinglong)
        os.umask(0o077)
        return asyncio.run(execute(args, config, qinglong=qinglong))
    except KeyboardInterrupt:
        return 130
    except AutoGrabError as error:
        print(json.dumps({"status": "BLOCKED", "reason": error.code, "live": "OFF", "arm": "OFF"}))
        return 2
    except (ValueError, TypeError, OSError):
        print('{"status":"FAILED","reason":"LOCAL_CONFIG_OR_STORAGE_ERROR","live":"OFF","arm":"OFF"}')
        return 1


def dispatch(argv):
    # Only the existing --root global option may precede a command.
    index = 2 if len(argv) > 1 and argv[0] == "--root" else 1 if argv and argv[0].startswith("--root=") else 0
    if len(argv) > index and argv[index] in {*NAMES, "all"}:
        return _run(parser().parse_args(argv))
    return None
