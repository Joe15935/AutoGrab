"""Five explicit factories; no dynamic plugins or authenticated browser fallback."""
from autograb.core.errors import AutoGrabError

NAMES = ("bandwagon", "dmit", "vmiss", "vps", "apple")


def create_provider(name, config, log, *, store=None):
    budget = None
    if store is not None and name in {"vmiss", "apple"}:
        from autograb.core.rate_budget import ProviderRateBudget
        budget = ProviderRateBudget(store)
    if name == "bandwagon":
        from .bandwagon import BandwagonHostProvider
        return BandwagonHostProvider(None, log)
    if name == "dmit":
        from .dmit import DMITProvider
        return DMITProvider(log=log)
    if name == "vmiss":
        from .vmiss import VMISSProvider
        return VMISSProvider(log=log, settings=config.providers.get("vmiss", {}), budget=budget)
    if name == "vps":
        from .vps import VPSProvider
        return VPSProvider(log=log)
    if name == "apple":
        from .apple import AppleProvider
        return AppleProvider(settings=config.providers.get("apple", {}), log=log, budget=budget)
    raise ValueError("Unknown provider")
