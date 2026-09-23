"""Small notification facade over the existing TLS SMTP implementation."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from .email import EmailNotifier, NotificationResult, safe_public_url
from .setup import load_setup


def notification_url(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    # A manually configured pickup target may have only a public store landing
    # URL. Accept those exact links for email without broadening Edge's contract.
    from autograb.providers.apple import REGIONS
    if url in {base + "/shop" for base in REGIONS.values()}:
        return url
    if safe_public_url(url) == url:
        return url
    from autograb.edge.protocol import public_url
    for provider in ("dmit", "vmiss", "vps", "apple"):
        try:
            if public_url(url, provider=provider, product=True) == url:
                return url
        except (ValueError, TypeError):
            continue
    return None


class NotificationAdapter:
    def __init__(self, notifier):
        self.email = notifier

    async def notify(self, title, body, url=None, priority=None, *, event_id=None):
        if result := self.email._configuration_result():
            return result
        if (not isinstance(title, str) or not 1 <= len(title) <= 200 or
                any(ord(c) < 32 for c in title) or not isinstance(body, str) or len(body) > 50000 or
                priority not in (None, "normal", "high")):
            return NotificationResult("NOTIFICATION_FAILED", "INVALID_MESSAGE", "Invalid notification")
        # This facade reports opportunities only. Payment-ready email retains
        # its separate verified invoice contract in EmailNotifier.
        if "PAY NOW" in title.upper() or "PAYMENT_READY" in title.upper():
            return NotificationResult("NOTIFICATION_FAILED", "INVALID_MESSAGE", "Use verified payment-ready path")
        if url:
            safe = notification_url(url)
            if not safe or safe != url:
                return NotificationResult("NOTIFICATION_FAILED", "INVALID_MESSAGE", "Invalid public URL")
            body += "\n\n" + safe
        message = self.email._message(title, body, event_id or "notice:" + str(uuid4()))
        if priority == "high":
            message["Importance"] = "high"
        return await asyncio.to_thread(self.email._send, message)

    async def send_event(self, product, event, timing, boundary):
        body = (f"Provider: {product.provider}\nProduct: {product.name}\nProduct ID: {product.product_id}\n"
                f"Opportunity: {', '.join(event.get('details', {}).get('opportunities', []))}\n"
                f"Price / billing: {json.dumps(product.prices, ensure_ascii=False)}\n"
                f"Stock: {product.availability}\nObserved: {event.get('created_at', 'UNKNOWN')}\n"
                "Mode: MONITOR\nNo order or payment was created. Inventory is not reserved.")
        if product.provider == "apple":
            body += "\nPickup: " + json.dumps(product.metadata.get("inventory", []), ensure_ascii=False)
        link = notification_url(product.product_url)
        return await self.notify(f"[AutoGrab Opportunity] {product.provider}", body,
            url=link, priority="high", event_id=f"{product.provider}:{event['id']}")


async def notify(title, body, url=None, priority=None, *, root=None):
    """Simple async API; credentials are loaded only by the existing SMTP path."""
    from autograb.core.config import Config
    config = Config.load(Path(root or os.environ.get("AUTOGRAB_ROOT", Path.cwd())).resolve())
    return await NotificationAdapter(EmailNotifier(load_setup(config.root, base=config.smtp))).notify(
        title, body, url, priority)
