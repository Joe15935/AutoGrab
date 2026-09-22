"""Apple-only catalog refresh and a current-official-data configuration wizard."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import tomllib

from autograb.core.errors import AutoGrabError
from autograb.core.lock import ProcessLock
from autograb.providers.apple import AppleProvider, REGIONS
from autograb.providers.apple_catalog import AppleCatalog, merge_observations
from autograb.storage.database import Store


REGION_LABELS = {"cn":"中国大陆", "us":"United States", "hk":"香港", "tw":"台灣",
                 "jp":"日本", "sg":"Singapore", "au":"Australia", "my":"Malaysia"}


def _choose(prompt, choices, *, input_fn=input, output=print, multiple=False):
    if not choices:
        raise AutoGrabError("APPLE_SELECTION_UNVERIFIED")
    output(prompt)
    for index, (label, _value) in enumerate(choices, 1):
        output(f"  {index}. {label}")
    while True:
        raw = input_fn("选择编号" + ("（多个用逗号分隔）" if multiple else "") + "，q 退出: ").strip()
        if raw.lower() == "q":
            raise AutoGrabError("APPLE_CONFIGURE_CANCELLED")
        values = raw.split(",") if multiple else [raw]
        if len(values) <= 20 and all(re.fullmatch(r"[0-9]{1,3}", value.strip()) for value in values):
            indexes = list(dict.fromkeys(int(value.strip()) for value in values))
            if all(1 <= index <= len(choices) for index in indexes):
                result = [choices[index-1][1] for index in indexes]
                return result if multiple else result[0]
        output("请选择列表中有效的编号。")


def _safe_existing(path):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise AutoGrabError("APPLE_CONFIG_PATH_UNSAFE")


def save_target(root, settings):
    """Write a private Apple-only overlay, never rewrite the main/SMTP config."""
    directory, name = Path(root) / "config", "apple.local.toml"
    if directory.is_symlink():
        raise AutoGrabError("APPLE_CONFIG_PATH_UNSAFE")
    directory.mkdir(exist_ok=True)
    path = directory / name
    _safe_existing(path)
    def scalar(value):
        if type(value) is bool: return "true" if value else "false"
        if isinstance(value,str): return json.dumps(value,ensure_ascii=False)
        if type(value) in (int,float): return json.dumps(value,allow_nan=False)
        if isinstance(value,list) and all(not isinstance(v,(dict,list)) for v in value):
            return "[" + ", ".join(scalar(v) for v in value) + "]"
        raise AutoGrabError("APPLE_CONFIG_VALUE_UNSUPPORTED")
    lines = ["# Local Apple targets. Main config and SMTP settings are unchanged.", "[apple]"]
    for key,value in settings.items():
        if key == "targets": continue
        if not re.fullmatch(r"[a-z_]+",key): raise AutoGrabError("APPLE_CONFIG_VALUE_UNSUPPORTED")
        if value is not None: lines.append(f"{key} = {scalar(value)}")
    for target in settings.get("targets",[]):
        lines.extend(["", "[[apple.targets]]"])
        for key,value in target.items():
            if not re.fullmatch(r"[a-z_]+",key): raise AutoGrabError("APPLE_CONFIG_VALUE_UNSUPPORTED")
            if value is not None: lines.append(f"{key} = {scalar(value)}")
    text = "\n".join(lines) + "\n"
    if tomllib.loads(text).get("apple") != {k:v for k,v in settings.items() if v is not None}:
        # Targets supplied here contain scalar fields only and omit null values.
        raise AutoGrabError("APPLE_CONFIG_VALIDATION_FAILED")
    if path.exists():
        backup = directory / (name + "." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".bak")
        with backup.open("xb") as handle:
            os.chmod(backup,0o600); handle.write(path.read_bytes())
    descriptor, temporary = tempfile.mkstemp(prefix=".apple-local-",dir=directory)
    try:
        with os.fdopen(descriptor,"w",encoding="utf-8") as handle:
            os.fchmod(handle.fileno(),0o600); handle.write(text); handle.flush(); os.fsync(handle.fileno())
        _safe_existing(path)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return path


def configure(config, *, region=None, input_fn=input, output=print, catalog_factory=AppleCatalog, budget=None):
    settings = dict(config.providers.get("apple",{}))
    region = region or _choose("Apple Store 地区",[(REGION_LABELS[k],k) for k in REGIONS],input_fn=input_fn,output=output)
    catalog = catalog_factory(region,progress=lambda _url:output("正在读取 Apple 官方目录…"), **({"budget":budget} if budget is not None else {}))
    categories = catalog.categories()
    category = _choose("商品类别（来自当前官网）",[(key,key) for key in categories],input_fn=input_fn,output=output)
    products = catalog.refresh([category])
    candidates = [p for p in products if p.metadata.get("pickup_configuration_verified")]
    if not candidates:
        raise AutoGrabError("APPLE_CATEGORY_CONFIGURATION_UNVERIFIED")
    for field, prompt in (("model","型号"),("capacity","容量"),("color","颜色"),("carrier","运营商")):
        values = list(dict.fromkeys(p.metadata["catalog_variant"].get(field) for p in candidates))
        if values == [None] or values == ["NOT_APPLICABLE"]:
            output(prompt + "：官网此配置未提供独立选项" if values == [None] else prompt + "：不适用")
            continue
        selected = _choose(prompt,[(value or "官网未提供",value) for value in values],input_fn=input_fn,output=output)
        candidates = [p for p in candidates if p.metadata["catalog_variant"].get(field) == selected]
    if len(candidates) > 1:
        keys = sorted({key for p in candidates for key in p.metadata["catalog_variant"]["dimensions"]})
        for key in keys:
            values = list(dict.fromkeys(p.metadata["catalog_variant"]["dimensions"].get(key) for p in candidates))
            if len(values) > 1:
                selected = _choose("其他官方配置："+key,[(str(value),value) for value in values],input_fn=input_fn,output=output)
                candidates = [p for p in candidates if p.metadata["catalog_variant"]["dimensions"].get(key) == selected]
    if len(candidates) != 1:
        raise AutoGrabError("APPLE_TARGET_VARIANT_AMBIGUOUS")
    product, stores = candidates[0], catalog.stores()
    selected = _choose("自提门店",[(f"{s['city']} · {s['name']}",s["store_id"]) for s in stores],input_fn=input_fn,output=output,multiple=True)
    variant = product.metadata["catalog_variant"]
    target = {"sku":product.product_id.split(":",1)[1], "product_url":product.product_url,
        "product_family":category, "model":variant["model"], "storage":variant["capacity"],
        "color":variant["color"], "carrier":variant["carrier"], "stores":selected, "modes":["pickup"]}
    target = {k:v for k,v in target.items() if v is not None}
    targets = list(settings.get("targets",[]))
    if targets and settings.get("region") != region:
        output("所选地区不同；保存将替换 Apple 目标，旧本地配置会保留备份。")
        targets = []
    targets = [t for t in targets if t.get("sku") != target["sku"]] + [target]
    settings.update(enabled=True,region=region,targets=targets,catalog_enabled=True,
        catalog_categories=list(dict.fromkeys([*(settings.get("catalog_categories",[]) if settings.get("region")==region else []),category])),catalog_refresh_seconds=3600)
    preview = AppleProvider({**settings,"catalog_enabled":False})
    if preview.status != "CONFIGURED":
        raise AutoGrabError(preview.status)
    output(f"目标：{product.name}；门店：" + "、".join(s["name"] for s in stores if s["store_id"] in selected))
    if input_fn("保存本地配置并进行一次只读库存检查？[y/N]: ").strip().lower() != "y":
        raise AutoGrabError("APPLE_CONFIGURE_CANCELLED")
    path = save_target(config.root,settings)
    output(f"配置已保存：{path.name}。不会创建订单或付款。")
    return settings, products


async def run_apple(args, config):
    from autograb.core.rate_budget import ProviderRateBudget
    with ProcessLock(config.root / "data/autograb.lock"), Store(config.root / "data/autograb.sqlite3") as store:
        return await _run_apple(args, config, store, ProviderRateBudget(store))


async def _run_apple(args, config, store, budget):
    from autograb.multi_cli import process_opportunity
    from autograb.notifications.email import EmailNotifier
    from autograb.notifications.setup import load_setup
    if args.command == "apple-configure":
        settings, products = await asyncio.to_thread(configure,config,region=args.region,budget=budget)
        # This is a user-selected target check, not a fabricated opportunity.
        provider = AppleProvider({**settings,"catalog_enabled":False}, budget=budget)
        observed = await provider.discover_products()
        print(json.dumps({"event":"APPLE_TARGET_CONFIGURED","inventory_status":provider.status,
            "inventory":provider.inventory,"orders_created":0,"payments":0},ensure_ascii=False))
    else:
        settings = config.providers.get("apple",{})
        region = args.region or settings.get("region")
        categories = args.category or settings.get("catalog_categories")
        if not region or not categories:
            raise AutoGrabError("APPLE_CATALOG_SCOPE_NOT_CONFIGURED")
        products = await asyncio.to_thread(AppleCatalog(region, budget=budget).refresh,categories)
        observed = []
    merged = merge_observations([*products,*observed],store.list_products())
    snapshot = store.ingest(merged,source="APPLE_CATALOG_REFRESH")
    if args.command == "apple-catalog-refresh":
        notifier = EmailNotifier(load_setup(config.root,base=config.smtp))
        provider = AppleProvider(settings, budget=budget)
        for event in snapshot["events"]:
            await process_opportunity(store,provider,event,notifier,prepare_checkout=False)
    else:
        for event in snapshot["events"]:
            store.update_event(event["id"],"RECORDED_ONLY",{"reason":"EXPLICIT_APPLE_CONFIGURATION"})
    print(json.dumps({"event":"APPLE_CATALOG_REFRESH","catalog_count":len(products),
        "opportunities":sum(bool(e["details"].get("opportunities")) for e in snapshot["events"]),
        "inventory_checked":bool(observed),"orders_created":0,"payments":0},ensure_ascii=False))
    return 0
