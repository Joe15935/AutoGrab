import argparse
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from autograb.browser.manager import BrowserManager
from autograb.core.config import Config
from autograb.core.errors import AutoGrabError
from autograb.core.events import EventLog
from autograb.core.lock import ProcessLock
from autograb.core.runner import Runner
from autograb.core.state import State, transition
from autograb.notifications.email import EmailNotifier, SMTPConfig
from autograb.providers.bandwagon import BandwagonHostProvider, CATALOG_PAGE
from autograb.storage.database import Store
from autograb.notifications.setup import load_setup
from autograb.edge.protocol import ProtocolError


def parser():
    p = argparse.ArgumentParser(description="AutoGrab — multi-provider / default DRY RUN + DISARMED")
    p.add_argument("--root", type=Path, default=Path.cwd(), help="Project data/config directory")
    commands = p.add_subparsers(dest="command", required=True)
    for name in ("baseline", "probe", "status", "history", "email-test", "open-session"):
        command = commands.add_parser(name)
        if name in {"baseline", "probe"}:
            command.add_argument("--provider", choices=["all", "bandwagon", "dmit", "vmiss", "vps", "apple"], default="all")
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--once", action="store_true")
    monitor.add_argument("--provider", choices=["all", "bandwagon", "dmit", "vmiss", "vps", "apple"], default="all")
    monitor.add_argument("--prepare-checkout", action="store_true", help="Queue verified opportunities to normal Edge, stopping before final order")
    for name in ("dry-run", "validate-live"):
        command = commands.add_parser(name)
        command.add_argument("--product-id", default="87")
    simulated = commands.add_parser("simulate")
    simulated.add_argument("provider", choices=["bandwagon"])
    simulated.add_argument("event", choices=["new-product", "restock"])
    simulated.add_argument("--product-id", default="87")
    from autograb.providers.apple import REGIONS
    for name in ("apple-configure", "apple-catalog-refresh"):
        command = commands.add_parser(name)
        command.add_argument("--region", choices=list(REGIONS))
        if name == "apple-catalog-refresh":
            command.add_argument("--category", action="append", help="Official category such as iphone; defaults to the configured catalog scope")
    from autograb.phase2_cli import register_commands
    register_commands(commands)
    from autograb.edge_cli import register_commands as register_edge_commands
    register_edge_commands(commands)
    from autograb.order_cli import register_commands as register_order_commands
    register_order_commands(commands)
    return p


async def keep_open(browser, log, reason):
    if not browser.context:
        return
    log.write("BROWSER_HELD", reason=reason, instruction="Browser is kept open for manual inspection. Close its windows or press Ctrl-C to end; no automatic retry.")
    while browser.context and browser.context.pages:
        await asyncio.sleep(1)


async def run(args, config):
    browser = BrowserManager(config)
    log = EventLog(config.root / "logs/events.jsonl")
    notifier = EmailNotifier(load_setup(config.root, base=config.smtp))
    with ProcessLock(config.root / "data/autograb.lock"), Store(config.root / "data/autograb.sqlite3") as store:
        recovered = store.recover_interrupted()
        log.write("START", version="0.5.0a0", database="OK", email="CONFIGURED" if notifier.configured else "NOT_CONFIGURED", recovered_events=recovered)
        if args.command in {"status", "history"}:
            print(json.dumps(store.summary() if args.command == "status" else store.list_events(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "email-test":
            result = await notifier.send_test()
            log.write("EMAIL_TEST", **asdict(result))
            return 0 if result.status == "SMTP_ACCEPTED" else 2
        provider = BandwagonHostProvider(browser, log)
        runner = Runner(store, provider, notifier, log)
        run_id = store.start_run(args.command)
        try:
            if args.command == "open-session":
                from autograb.browser.login_policy import HumanLoginPolicy
                browser.policy = HumanLoginPolicy()
                await browser.start()
                await browser.navigate(browser.page, "https://bandwagonhost.com/clientarea.php")
                await keep_open(browser, log, "HUMAN_SESSION_LOGIN")
                store.finish_run(run_id, "COMPLETE")
                return 0
            if args.command == "probe":
                products = await provider.discover_products()
                log.write("PROBE", known_count=len(products), routes=provider.routes, selected_source=provider.source)
                store.finish_run(run_id, "COMPLETE")
                return 0
            if args.command in {"baseline", "monitor", "validate-live"}:
                monitor_state = transition(State.IDLE, State.MONITORING)
                failures = 0
                while True:
                    from autograb.core.live import signal_present
                    if signal_present(config.root / "data", "stop_monitoring"):
                        log.write("MONITORING_STOPPED")
                        break
                    try:
                        products = await provider.discover_products()
                        snapshot = store.ingest(products, source=provider.source)
                        if snapshot["baseline_initialized"]:
                            monitor_state = transition(monitor_state, State.BASELINE)
                            log.write("BASELINE_INITIALIZATION", known_count=snapshot["known_count"], triggered=0, state=monitor_state.value)
                            monitor_state = transition(monitor_state, State.MONITORING)
                        else:
                            log.write("MONITORING", known_count=snapshot["known_count"], triggered=len(snapshot["events"]))
                        # Drain only events from this observation. Never replay stale queued events on restart.
                        if args.command == "monitor":
                            for event in snapshot["events"]:
                                product = event["product"]
                                if product.get("eligible") and product.get("availability") == "AVAILABLE":
                                    result = await runner.process(event)
                                    if result["status"] != "COMPLETE":
                                        log.write("ARTIFACT", **await browser.artifact(result.get("error_code", "UNKNOWN")))
                                        if result["status"] in {"CAPTCHA_REQUIRED", "LOGIN_REQUIRED"}:
                                            await keep_open(browser, log, result["status"])
                                            raise AutoGrabError(result["status"])
                                else:
                                    store.update_event(event["id"], "RECORDED_ONLY", {"reason": "NO_AVAILABLE_TARGET_EVENT"})
                        else:
                            for event in snapshot["events"]:
                                store.update_event(event["id"], "RECORDED_ONLY", {"reason": "EXPLICIT_BASELINE_COMMAND"})
                        failures = 0
                    except AutoGrabError as error:
                        if args.command != "monitor" or args.once or error.code in {"CAPTCHA_REQUIRED", "CLOUDFLARE", "HTTP_403", "LOGIN_REQUIRED"}:
                            raise
                        failures += 1
                        log.write("MONITOR_ERROR", error_code=error.code, attempt=failures)
                        if failures >= 3:
                            raise
                    if args.command != "monitor" or args.once:
                        break
                    from autograb.phase2_cli import monitoring_wait
                    if not await monitoring_wait(config.root / "data", min(config.normal_interval * (2 ** failures), 300)):
                        break
                if args.command in {"baseline", "monitor"}:
                    store.finish_run(run_id, "COMPLETE", store.summary())
                    return 0
            tests = [None, "NEW_PRODUCT", "RESTOCK"] if args.command == "validate-live" else [args.event.upper().replace("-", "_") if args.command == "simulate" else None]
            results = []
            for simulated_kind in tests:
                product = await provider.check_product(args.product_id)
                event = store.create_event(product, simulated_kind or "PRODUCT_CHANGED", simulated=simulated_kind is not None)
                log.write("SIMULATED" if simulated_kind else "DRY_RUN_PRODUCT_TEST", event_id=event["id"], product_id=product.product_id, event_type=event["event_type"])
                result = await runner.process(event)
                results.append(result)
                if result["status"] != "COMPLETE":
                    log.write("ARTIFACT", **await browser.artifact(result.get("error_code", "UNKNOWN")))
                    if result["status"] in {"CAPTCHA_REQUIRED", "LOGIN_REQUIRED"}:
                        await keep_open(browser, log, result["status"])
                    break
            success = len(results) == len(tests) and all(r["status"] == "COMPLETE" for r in results)
            store.finish_run(run_id, "COMPLETE" if success else "FAILED", {"results": results, "routes": provider.routes})
            return 0 if success else 1
        except AutoGrabError as error:
            log.write("FAILED", error_code=error.code)
            log.write("ARTIFACT", **await browser.artifact(error.code))
            store.finish_run(run_id, "FAILED", {"error_code": error.code, "routes": provider.routes})
            if (error.code in {"CAPTCHA_REQUIRED", "CLOUDFLARE", "LOGIN_REQUIRED"}
                    or (args.command == "open-session" and error.code == "HTTP_403")):
                await keep_open(browser, log, error.code)
            return 1
        except asyncio.CancelledError:
            store.finish_run(run_id, "INTERRUPTED")
            raise
        except Exception:
            log.write("FAILED", error_code="UNKNOWN")
            log.write("ARTIFACT", **await browser.artifact("UNKNOWN"))
            store.finish_run(run_id, "FAILED", {"error_code": "UNKNOWN"})
            return 1
        finally:
            await browser.close()


def main():
    os.umask(0o077)
    args = parser().parse_args()
    try:
        config = Config.load(args.root.resolve())
        config.prepare()
        if getattr(args, "order_core", False):
            from autograb.order_cli import run_order
            return asyncio.run(run_order(args, config))
        if args.command in {"apple-configure", "apple-catalog-refresh"}:
            from autograb.apple_cli import run_apple
            return asyncio.run(run_apple(args, config))
        # All supported authenticated entry points migrate to the ordinary Edge
        # Companion. Legacy helpers remain only for historical regression tests.
        from autograb.edge_cli import handles, run_edge
        if handles(args):
            return asyncio.run(run_edge(args, config))
        if getattr(args, "phase2", False):
            from autograb.phase2_cli import run_phase2
            return asyncio.run(run_phase2(args, config))
        if args.command in {"baseline", "probe", "monitor"}:
            from autograb.multi_cli import run_multi
            return asyncio.run(run_multi(args, config))
        return asyncio.run(run(args, config))
    except KeyboardInterrupt:
        print("AutoGrab stopped. Saved baseline and event history retained.")
        return 130
    except AutoGrabError as error:
        print(json.dumps({"status": "FAILED", "error_code": error.code}))
        return 1
    except ProtocolError as error:
        print(json.dumps({"status": "PAUSED", "error_code": str(error), "live": "OFF"}))
        return 2
    except (ValueError, TypeError, OSError):
        print('{"status":"FAILED","error_code":"LOCAL_CONFIG_OR_STORAGE_ERROR"}')
        return 1
