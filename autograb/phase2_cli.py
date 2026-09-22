"""Small Phase 2 controls. Restart never restores LIVE permission."""
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math

from .browser.manager import BrowserManager
from .core.errors import AutoGrabError
from .core.events import EventLog
from .core.live import (LiveGuard, Preflight, SMTPProof, signal_disarm,
                        signal_stop_monitoring, signal_present, clear_signal)
from .core.lock import ProcessLock
from .core.purchase_state import PurchaseState
from .notifications.email import EmailNotifier
from .notifications.setup import load_setup, setup_email
from .providers.checkout import BandwagonPaymentProvider
from .storage.database import Store
from .storage.intents import IntentStore


def register_commands(commands):
    for name in ("configure", "configure-email", "disarm", "stop", "stop-all", "resume-monitoring", "clear-disarm", "control-status", "intents", "reconcile"):
        commands.add_parser(name).set_defaults(phase2=True)
    preflight = commands.add_parser("preflight")
    preflight.set_defaults(phase2=True)
    preflight.add_argument("--test-email", action="store_true")
    preflight.add_argument("--offline", action="store_true", help="Report local readiness only; no network")
    arm = commands.add_parser("arm", help="Arm and monitor in this foreground process only")
    arm.set_defaults(phase2=True)
    arm.add_argument("--mode", choices=["DRY_RUN", "LIVE"], default="DRY_RUN")
    expiry = arm.add_mutually_exclusive_group()
    expiry.add_argument("--hours", type=float, default=None)
    expiry.add_argument("--until", help="ISO date/time including timezone")


async def monitoring_wait(data_dir, seconds):
    end = asyncio.get_running_loop().time() + seconds
    while not signal_present(data_dir, "stop_monitoring"):
        remaining = end - asyncio.get_running_loop().time()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(1, remaining))
    return False


async def collect_preflight(store, provider, notifier, *, test_email=False, offline=False):
    checks = {name: "NOT_TESTED" for name in ("database", "browser", "site", "session", "baseline", "email", "boundary")}
    checks["database"] = "PASS" if store.connection.execute("PRAGMA quick_check").fetchone()[0] == "ok" else "FAIL"
    checks["baseline"] = "PASS" if store.summary()["baseline_initialized"] else "NOT_READY"
    checks["email"] = "CONFIGURED_UNTESTED" if notifier.configured else "NOT_CONFIGURED"
    checks["boundary"] = "UNVERIFIED"
    smtp_proof = None
    if not offline:
        await provider.discover_products()
        checks["browser"] = checks["site"] = "PASS"
        session = await provider.inspect_session()
        checks["session"] = "PASS" if session["status"] == "SESSION_VALID" else session["status"]
        # A boolean in config cannot satisfy this account-specific capability.
        checks["boundary"] = "UNVERIFIED_AUTHENTICATED_ACCOUNT"
        if test_email and notifier.configured:
            result = await notifier.send_live_test()
            checks["email"] = "PASS" if result.status == "SMTP_ACCEPTED" else result.status
            if result.status == "SMTP_ACCEPTED":
                smtp_proof = SMTPProof("SMTP_ACCEPTED", "REAL_SMTP", datetime.now(timezone.utc))
    return Preflight(checks, smtp_proof=smtp_proof)


def display_preflight(preflight, guard):
    print(json.dumps({"event": "LIVE_PRE_FLIGHT", "checks": dict(preflight.checks),
                      "ready": not preflight.failures(datetime.now(timezone.utc)),
                      "control": guard.status()}, ensure_ascii=False, indent=2))


def _recovery_succeeded(results):
    """Command success means every pending result was actually resolved."""
    if not isinstance(results, list):
        return False
    for result in results:
        if not isinstance(result, dict):
            return False
        status, error = result.get("status"), result.get("error_code")
        if status == "ORDER_SUBMIT_FAILED" and error == "ORDER_ABSENCE_CONFIRMED_NO_AUTORETRY":
            continue
        if status not in {"PAYMENT_READY", "WAITING_FOR_USER"} or error:
            return False
        notification = result.get("notification")
        if not isinstance(notification, dict) or notification.get("status") != "SMTP_ACCEPTED":
            return False
    return True


def _expiry(args):
    try:
        if args.until:
            expiry = datetime.fromisoformat(args.until)
            if expiry.tzinfo is None or expiry.utcoffset() is None:
                raise ValueError
            duration = expiry - datetime.now(timezone.utc)
            if not timedelta(0) < duration <= timedelta(hours=24):
                raise ValueError
            return {"armed_until": expiry}
        hours = args.hours if args.hours is not None else 1
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or not 0 < hours <= 24:
            raise ValueError
        return {"duration": timedelta(hours=hours)}
    except (ValueError, TypeError, OverflowError):
        raise AutoGrabError("ARM_EXPIRY_INVALID") from None


async def run_phase2(args, config):
    data = config.root / "data"
    command = args.command
    # These controls intentionally bypass the main process lock.
    if command in {"disarm", "stop", "stop-all"}:
        if command in {"disarm", "stop-all"}:
            signal_disarm(data)
        if command in {"stop", "stop-all"}:
            signal_stop_monitoring(data)
        print(json.dumps({"command": command, "status": "SIGNAL_WRITTEN", "existing_orders": "PRESERVED"}))
        return 0
    if command in {"resume-monitoring", "clear-disarm"}:
        clear_signal(data, "stop_monitoring" if command == "resume-monitoring" else "disarm", explicit=True)
        print("Signal cleared. LIVE remains DISARMED; no process was started.")
        return 0
    if command in {"configure", "configure-email"}:
        if command == "configure":
            print("AutoGrab 配置\n1. 配置邮件并测试\n2. 打开搬瓦工专用登录窗口\n3. 查看本地状态\n0. 退出")
            choice = input("选择 [0]: ").strip()
            if choice == "2":
                # Reuse the existing human-only session command without copying credentials.
                from .main import run
                from argparse import Namespace
                return await run(Namespace(command="open-session"), config)
            if choice == "3":
                args.command = "control-status"
                return await run_phase2(args, config)
            if choice != "1":
                return 0
        # Setup requires exclusive local access but has no browser/order operation.
        with ProcessLock(data / "autograb.lock"):
            result = await setup_email(config.root)
            print(json.dumps(asdict(result), ensure_ascii=False))
            if result.status == "CANCELLED":
                return 0
            if result.status == "CONFIGURED":
                return 0 if result.notification is None or result.notification.status == "SMTP_ACCEPTED" else 2
            return 2
    browser = BrowserManager(config)
    log = EventLog(config.root / "logs/events.jsonl", mode=getattr(args, "mode", "DRY_RUN"))
    provider = BandwagonPaymentProvider(browser, log, preferred_payment_gateway=config.bandwagon.get("preferred_payment_gateway"))
    notifier = EmailNotifier(load_setup(config.root, base=config.smtp))
    guard = LiveGuard(data, mode=getattr(args, "mode", "DRY_RUN"))
    with ProcessLock(data / "autograb.lock"), Store(data / "autograb.sqlite3") as store:
        intents = IntentStore(store)
        intents.recover_interrupted()
        if command == "control-status":
            print(json.dumps({"scope": "THIS_COMMAND_PROCESS", "control": guard.status(),
                              "cross_process_arm_status": "NOT_OBSERVED", "dashboard": "NOT_IMPLEMENTED",
                              "monitoring_stop_requested": signal_present(data, "stop_monitoring"),
                              "email": "CONFIGURED_NOT_TESTED" if notifier.configured else "NOT_CONFIGURED",
                              "session_status": "NOT_TESTED", "order_adapter": "AUTHENTICATED_BOUNDARY_UNVERIFIED",
                              "baseline": store.summary()["known_count"], "active_intents": len(intents.list(active_only=True))}, indent=2))
            print("这里只显示本次命令的权限及本地持久信号；没有 Dashboard，也未查询其他运行进程的 ARM 状态。")
            return 0
        if command == "intents":
            # Local private CLI only; no IDs, URLs or account data enter reports/logs.
            print(json.dumps(intents.list(), ensure_ascii=False, indent=2))
            return 0
        try:
            if command == "preflight":
                preflight = await collect_preflight(store, provider, notifier, test_email=args.test_email, offline=args.offline)
                display_preflight(preflight, guard)
                return 0 if not preflight.failures(datetime.now(timezone.utc)) else 2
            from .core.purchase import PurchaseRunner
            async def current_preflight():
                current = await collect_preflight(store, provider, notifier)
                return Preflight(current.checks | {"email": "PASS" if proof else "NOT_READY"}, smtp_proof=proof)
            proof = None
            runner = PurchaseRunner(store, provider, notifier, log, guard, current_preflight)
            if command == "reconcile":
                result = await runner.recover()
                resolved = _recovery_succeeded(result)
                print(json.dumps({"status": "RECONCILIATION_COMPLETE" if resolved else "RECONCILIATION_REQUIRED",
                                  "results": result, "automatic_resubmission": "DISABLED",
                                  "order_adapter": "AUTHENTICATED_BOUNDARY_UNVERIFIED"}, ensure_ascii=False, indent=2))
                return 0 if resolved else 2
            if command == "arm":
                if guard.mode != "LIVE":
                    raise AutoGrabError("DRY_RUN_CANNOT_ARM")
                if signal_present(data, "stop_monitoring"):
                    raise AutoGrabError("MONITORING_STOPPED")
                if not notifier.configured:
                    raise AutoGrabError("EMAIL_NOT_CONFIGURED")
                expiry = _expiry(args)
                preflight = await collect_preflight(store, provider, notifier, test_email=True)
                display_preflight(preflight, guard)
                proof = preflight.smtp_proof
                guard.arm(preflight, **expiry)
                # This remains unreachable for the real adapter until its authenticated boundary is reviewed.
                print(json.dumps(guard.status()))
                while guard.status()["armed"] and not signal_present(data, "stop_monitoring"):
                    snapshot = store.ingest(await provider.discover_products(), source=provider.source)
                    for event in snapshot["events"]:
                        if not guard.status()["armed"] or signal_present(data, "stop_monitoring"):
                            return 0
                        if event["event_type"] in {"NEW_PRODUCT", "RESTOCK", "PROMOTIONAL_EVENT"} and event["product"].get("eligible"):
                            result = await runner.process(event)
                            status = result.get("status") if isinstance(result, dict) else None
                            status = status if isinstance(status, str) else None
                            if status in {"ALREADY_CLAIMED", "STALE_EVENT"}:
                                continue
                            # One actionable intent per ARM session. A login
                            # pause or unknown submission must not permit the
                            # next product in this batch to reach dispatch.
                            guard.disarm()
                            if status in {"PAYMENT_READY", "WAITING_FOR_USER"}:
                                print("WAITING_FOR_USER: existing order retained; no more submissions.")
                                while browser.context and browser.context.pages:
                                    await asyncio.sleep(1)
                                notification = result.get("notification", {})
                                return 0 if isinstance(notification, dict) and notification.get("status") == "SMTP_ACCEPTED" else 2
                            known_status = status if status in {item.value for item in PurchaseState} else "UNVERIFIED_PURCHASE_RESULT"
                            print(json.dumps({"status": "PURCHASE_ATTENTION_REQUIRED", "purchase_status": known_status,
                                              "control": guard.status(), "remaining_queue": "NOT_DISPATCHED",
                                              "automatic_resubmission": "DISABLED"}))
                            error_code = result.get("error_code") if isinstance(result, dict) else None
                            error_code = error_code if isinstance(error_code, str) else None
                            if status == "CAPTCHA_REQUIRED" or error_code in {"CAPTCHA_REQUIRED", "CLOUDFLARE"}:
                                print("CAPTCHA_REQUIRED: browser remains open. Close it or press Ctrl-C; no automatic retry.")
                                while browser.context and browser.context.pages:
                                    await asyncio.sleep(1)
                            return 2
                    if not await monitoring_wait(data, config.normal_interval):
                        break
                return 0
        except AutoGrabError as error:
            print(json.dumps({"status": "USER_ACTION_REQUIRED" if error.code in {"LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "ORDER_BOUNDARY_UNVERIFIED", "EMAIL_NOT_CONFIGURED"} else "REFUSED",
                              "error_code": error.code, "control": guard.status()}))
            guidance = {
                "EMAIL_NOT_CONFIGURED": "请先运行 configure-email，由本人配置并测试邮件；未验证邮件前不能 ARM。",
                "DRY_RUN_CANNOT_ARM": "当前是 DRY RUN，未启用真实订单权限。",
                "ARM_EXPIRY_INVALID": "ARM 有效期须为未来 24 小时以内；日期必须包含时区。",
                "LIVE_PREFLIGHT_FAILED": "预检未全部通过，LIVE 保持 DISARMED。真实账户的订单和扣款边界尚未验证。",
                "ORDER_BOUNDARY_UNVERIFIED": "真实账户的最终订单提交和扣款边界尚未验证；已停止，不会自动提交订单。",
                "LOGIN_REQUIRED": "请由本人运行 open-session 登录搬瓦工，再继续预检。",
                "MONITORING_STOPPED": "监控已被停止；resume-monitoring 只清除停止信号，不会自动 ARM。",
            }
            if error.code in guidance:
                print(guidance[error.code])
            if error.code in {"CAPTCHA_REQUIRED", "CLOUDFLARE"}:
                print("CAPTCHA_REQUIRED: browser remains open. Close it or press Ctrl-C; no automatic retry.")
                while browser.context and browser.context.pages:
                    await asyncio.sleep(1)
            return 2
        finally:
            guard.disarm()
            await browser.close()
    return 2
