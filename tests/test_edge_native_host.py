"""Exercise the actual host process over stdio, using private temporary state."""
import asyncio
import io
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from autograb.core.models import Product
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import encode, make_message, read_message
from autograb.notifications.email import EmailNotifier, NotificationResult, SMTPConfig
from autograb.storage.database import Store


REPO = Path(__file__).resolve().parents[1]
EXTENSION_ID = "a" * 32


class EdgeNativeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "config/edge-identity.json").write_text(json.dumps({"extension_id": EXTENSION_ID}))
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        self.temp.cleanup()

    def launch(self, origin=None):
        process = subprocess.Popen([sys.executable, "-m", "autograb.edge.native_host", "--root", str(self.root),
                                    "--extension-id", EXTENSION_ID,
                                    origin or f"chrome-extension://{EXTENSION_ID}/"],
                                   cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def send(self, process, message):
        process.stdin.write(encode(message))
        process.stdin.flush()

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail("Native host condition was not reached")

    def connected(self):
        path = self.root / "data/autograb.sqlite3"
        if not path.exists():
            return False
        with Store(path) as store:
            return EdgeBroker(store).status()["connected"]

    def handshake(self):
        process = self.launch()
        self.send(process, make_message("EDGE_READY", payload={"version": "0.2.1"}))
        self.wait_for(self.connected)
        return process

    def enqueue(self):
        with Store(self.root / "data/autograb.sqlite3") as store:
            broker = EdgeBroker(store)
            product = Product("87", "Fixture", "AVAILABLE", [], "https://bandwagonhost.com/order/ecommerce", eligible=True)
            event = store.create_event(product, "PRODUCT_CHANGED", simulated=False)
            intent = broker.intents.create(event, product)
            command = broker.enqueue("START_DRY_RUN", intent["intent_id"], "87", {"mode": "DRY_RUN", "product": {
                "name": "Fixture", "url": product.product_url, "period": "annually", "cents": 4999, "currency": "USD"}})
        return intent, command

    def read_command(self, process):
        ready, _, _ = select.select([process.stdout], [], [], 4)
        self.assertTrue(ready, "Host did not produce a framed command")
        return read_message(process.stdout, direction="command")

    def test_real_host_framing_handshake_queue_and_checkout_stop(self):
        process = self.handshake()
        intent, command = self.enqueue()
        received = self.read_command(process)
        self.assertEqual(received["command_id"], command["command_id"])
        for kind in ("PAGE_OPENED", "PRODUCT_VERIFIED", "CART_READY", "CHECKOUT_READY"):
            payload = {"tab_id": 42, "challenge": "NONE", "login": "VALID"}
            if kind == "PAGE_OPENED":
                payload["url"] = "https://bandwagonhost.com/order/ecommerce"
            if kind == "CART_READY":
                payload["cart_id"] = "0"
            self.send(process, make_message(kind, command_id=command["command_id"], intent_id=intent["intent_id"],
                                            product_id="87", payload=payload))
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(process.stdout.read(), b"")
        with Store(self.root / "data/autograb.sqlite3") as store:
            broker = EdgeBroker(store)
            saved = broker.intents.get(intent["intent_id"])
            self.assertEqual(saved["state"], "CHECKOUT_READY")
            self.assertIsNone(saved["order_id"])
            self.assertIsNone(saved["payment_url"])
            self.assertFalse(broker.status()["connected"])

    def test_wrong_origin_rejected_before_database_creation(self):
        process = self.launch("chrome-extension://" + "b" * 32 + "/")
        self.assertEqual(process.wait(timeout=5), 1)
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(process.stdout.read(), b"")

    def test_second_native_host_cannot_interrupt_first(self):
        first = self.handshake()
        second = self.launch()
        self.assertEqual(second.wait(timeout=5), 1)
        self.assertIsNone(first.poll())
        self.assertTrue(self.connected())

    def test_sigterm_during_partial_frame_stops_and_never_replays_command(self):
        process = self.handshake()
        intent, _ = self.enqueue()
        self.read_command(process)
        process.stdin.write(b"\xff")
        process.stdin.flush()
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 0)
        with Store(self.root / "data/autograb.sqlite3") as store:
            broker = EdgeBroker(store)
            self.assertEqual(broker.intents.get(intent["intent_id"])["edge_state"], "DISCONNECTED")
            self.assertEqual(store.connection.execute("SELECT status FROM edge_commands WHERE type='START_DRY_RUN'").fetchone()[0], "HALTED")
        self.handshake()
        with Store(self.root / "data/autograb.sqlite3") as store:
            self.assertIsNone(EdgeBroker(store).next_command())

    def test_new_kill_signal_is_observed_and_delivered_as_disarm(self):
        process = self.handshake()
        self.enqueue()
        self.read_command(process)
        (self.root / "data/disarm.signal").write_text("stop")
        message = self.read_command(process)
        self.assertEqual(message["type"], "DISARM")
        with Store(self.root / "data/autograb.sqlite3") as store:
            self.assertEqual(EdgeBroker(store).status()["execution_state"], "DISARMED")

    def test_invalid_frame_disconnects_without_logging_content(self):
        process = self.handshake()
        process.stdin.write(b"\xff\xff\xff\xffPRIVATE-CONTENT")
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=5), 1)
        self.assertNotIn(b"PRIVATE-CONTENT", process.stderr.read())
        self.assertFalse(self.connected())

    def test_edge_notice_uses_existing_mail_transport_and_normal_edge_instructions(self):
        config = SMTPConfig(host="smtp.example.com", port=465, username="test@example.com",
                            sender="test@example.com", recipient="to@example.com", _password="fixture")
        notifier = EmailNotifier(config)
        with patch.object(notifier, "_send", return_value=NotificationResult("SMTP_ACCEPTED")) as send:
            result = asyncio.run(notifier.send_edge_human_required(f"edge:{uuid4()}:1", "HUMAN_CHALLENGE_REQUIRED"))
        self.assertEqual(result.status, "SMTP_ACCEPTED")
        body = send.call_args.args[0].get_content()
        self.assertIn("Microsoft Edge", body)
        self.assertIn("订单结果以已保存的状态和商家核实结果为准", body)
        self.assertNotIn("本次仅为 DRY RUN", body)
        self.assertIn("自动付款保持关闭", body)
        self.assertNotIn("dedicated browser", body)


if __name__ == "__main__":
    unittest.main()
