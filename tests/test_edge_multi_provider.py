"""Provider isolation, migration preservation and read-only unknown-price paths."""
from argparse import Namespace
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from autograb.core.config import Config
from autograb.core.models import Product
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import ProtocolError, make_message, validate
from autograb.edge_cli import product_payload, run_edge
from autograb.storage.database import Store
from autograb.storage.intents import IntentStore

TARGETS = {
    "bandwagon": ("87", "https://bandwagonhost.com/order/ecommerce", "USD", "annually"),
    "dmit": ("87", "https://www.dmit.io/cart.php", "USD", "monthly"),
    "vmiss": ("us-los-angeles-bgp/basic", "https://app.vmiss.com/store/us-los-angeles-bgp", "CAD", "monthly"),
    "vps": ("87", "https://v.ps/products/cloud-kvm-vps/", "EUR", "monthly"),
    "apple": ("cn:MYEV3CH/A", "https://www.apple.com.cn/shop/buy-iphone/iphone-16/myev3ch/a", "CNY", "one_time"),
}


def product(provider="bandwagon", *, unknown=False):
    pid, url, currency, period = TARGETS[provider]
    return Product(pid, "Fixture", "UNKNOWN" if unknown else "AVAILABLE",
                   [] if unknown else [{"cents": 9900, "currency": currency, "period": period}], url,
                   eligible=True, provider=provider)


class MultiProviderProtocolTests(unittest.TestCase):
    def test_shared_python_javascript_identity_price_and_url_corpus(self):
        cases = []
        for provider in TARGETS:
            p = product(provider)
            base = make_message("START_DRY_RUN", provider=provider, intent_id=str(uuid4()), product_id=p.product_id, payload=product_payload(p))
            cases.append((provider, base, "command"))
            for other in TARGETS.keys() - {provider}:
                changed = deepcopy(base)
                changed["provider"] = other
                cases.append((provider + " wrong origin " + other, changed, "command"))
            if provider != "bandwagon":
                changed = deepcopy(base)
                changed["type"] = "OPEN_PRODUCT"
                changed["payload"]["product"].update(cents=None, currency=None, period="unknown")
                cases.append((provider + " diagnostic", changed, "command"))
                changed = deepcopy(changed)
                changed["type"] = "START_DRY_RUN"
                cases.append((provider + " unknown mutation", changed, "command"))
        for url in ["https://www.apple.com/shop/product/MYEV3CH/A",
                    "https://www.apple.com.cn/shop/product/DIFFERENT/A",
                    "https://www.apple.com.cn/shop/buy-iphone/iphone-16/myev3ch/a?token=private"]:
            changed = deepcopy(cases[-1][1])
            changed["type"] = "OPEN_PRODUCT"
            changed["payload"]["product"]["url"] = url
            cases.append(("Apple region/SKU/token", changed, "command"))
        for provider, url in [("dmit", "https://www.dmit.io/cart.php?a=checkout"),
                              ("vmiss", "https://app.vmiss.com/cart.php?a=checkout&token=private"),
                              ("vps", "https://vps.hosting/?cmd=cart&action=checkout"),
                              ("vps", "https://vps.hosting/?cmd=cart"),
                              ("apple", "https://www.apple.com.cn/shop/bag"),
                              ("apple", "https://idmsa.apple.com/")]:
            p = product(provider)
            event = make_message("PAGE_OPENED", provider=provider, intent_id=str(uuid4()), product_id=p.product_id, payload={})
            event["payload"]["url"] = url
            cases.append((provider + " event URL " + url, event, "event"))
        expected = []
        for _, value, direction in cases:
            try:
                validate(value, direction=direction)
                expected.append(True)
            except ProtocolError:
                expected.append(False)
        node = shutil.which("node")
        if not node:
            self.skipTest("Node required for shared protocol corpus")
        module = (Path(__file__).resolve().parents[1] / "edge-extension/protocol.js").as_uri()
        script = """import {readFileSync} from 'node:fs';
const {validateEnvelope} = await import(process.argv[1]);
process.stdout.write(JSON.stringify(JSON.parse(readFileSync(0,'utf8')).map(([,m,d]) => {
try { validateEnvelope(m,d); return true; } catch { return false; }})));"""
        response = subprocess.run([node, "--input-type=module", "-e", script, module], input=json.dumps(cases), text=True, capture_output=True, check=True)
        for (label, _, _), python_ok, js_ok in zip(cases, expected, json.loads(response.stdout)):
            with self.subTest(label=label):
                self.assertEqual(python_ok, js_ok)

    def test_unknown_price_diagnostic_cannot_navigate_add_action(self):
        p = product("dmit", unknown=True)
        payload = product_payload(p)
        payload["product"]["url"] = "https://www.dmit.io/cart.php?a=add&pid=87"
        with self.assertRaisesRegex(ProtocolError, "READ_ONLY_PRODUCT_URL_REQUIRED"):
            make_message("OPEN_PRODUCT", provider="dmit", intent_id=str(uuid4()), product_id="87", payload=payload)


LEGACY = """CREATE TABLE purchase_intents (
intent_id TEXT PRIMARY KEY, provider TEXT NOT NULL CHECK(provider='bandwagon'), product_id TEXT NOT NULL,
event_id TEXT NOT NULL UNIQUE REFERENCES events(id), origin TEXT NOT NULL CHECK(origin IN ('REAL','SIMULATED')),
created_at TEXT NOT NULL, updated_at TEXT NOT NULL, state TEXT NOT NULL, cart_identifier TEXT,
order_id TEXT, invoice_id TEXT, payment_url TEXT, payment_page_verified INTEGER NOT NULL DEFAULT 0,
submit_started_at TEXT, submit_finished_at TEXT, reconciliation_status TEXT, reconciled_at TEXT,
verification_json TEXT, terminal_evidence_json TEXT,
edge_state TEXT, edge_checkpoint TEXT, edge_command_id TEXT, edge_error_code TEXT,
edge_tab_id INTEGER, edge_mutation_uncertain INTEGER NOT NULL DEFAULT 0,
edge_pause_generation INTEGER NOT NULL DEFAULT 0, edge_normal_returned INTEGER NOT NULL DEFAULT 0,
future_column TEXT DEFAULT 'preserve-me')"""


class MultiProviderPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    def legacy(self):
        connection = self.store.connection
        event = self.store.create_event(product(), "PRODUCT_CHANGED")
        connection.execute(LEGACY)
        identity = str(uuid4())
        connection.execute("INSERT INTO purchase_intents(intent_id,provider,product_id,event_id,origin,created_at,updated_at,state,edge_state,edge_tab_id,edge_pause_generation) VALUES(?,'bandwagon','87',?,'REAL','old','old','CART_READY','WAITING_FOR_HUMAN',12,4)", (identity, event["id"]))
        connection.execute("CREATE INDEX custom_future_index ON purchase_intents(future_column)")
        connection.execute("CREATE TABLE migration_audit(value TEXT)")
        connection.execute("CREATE TRIGGER preserved_trigger AFTER UPDATE ON purchase_intents BEGIN INSERT INTO migration_audit VALUES(new.future_column); END")
        connection.execute("CREATE TABLE dependent_intent(intent_id TEXT REFERENCES purchase_intents(intent_id))")
        connection.execute("INSERT INTO dependent_intent VALUES(?)", (identity,))
        connection.execute("CREATE VIEW intent_view AS SELECT intent_id,future_column FROM purchase_intents")
        return identity

    def test_legacy_rebuild_preserves_all_data_foreign_keys_view_trigger_and_indexes(self):
        identity = self.legacy()
        connection = self.store.connection
        before = dict(connection.execute("SELECT rowid,* FROM purchase_intents").fetchone())
        EdgeBroker(self.store)
        after = dict(connection.execute("SELECT rowid,* FROM purchase_intents").fetchone())
        self.assertEqual({key: after[key] for key in before}, before)
        added = {'submission_nonce', 'order_precheck_json', 'order_created_at', 'payment_ready_at', 'notification_claimed_at'}
        self.assertEqual(set(after) - set(before), added)
        self.assertTrue(all(after[key] is None for key in added))
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(connection.execute("SELECT * FROM intent_view").fetchone()[0], identity)
        self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='custom_future_index'").fetchone())
        connection.execute("UPDATE purchase_intents SET future_column='retained' WHERE intent_id=?", (identity,))
        self.assertEqual(connection.execute("SELECT value FROM migration_audit").fetchone()[0], "retained")
        IntentStore(self.store)  # Idempotent reopen leaves existing data untouched.
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_intents").fetchone()[0], 1)

    def test_migration_conflict_rolls_back_and_restores_connection_pragmas(self):
        self.legacy()
        connection = self.store.connection
        original = connection.execute("SELECT sql FROM sqlite_master WHERE name='purchase_intents'").fetchone()[0]
        connection.execute("CREATE TABLE purchase_intents_provider_migration(user_data TEXT)")
        with self.assertRaisesRegex(ValueError, "SCHEMA_CONFLICT"):
            IntentStore(self.store)
        self.assertEqual(connection.execute("SELECT sql FROM sqlite_master WHERE name='purchase_intents'").fetchone()[0], original)
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM purchase_intents").fetchone()[0], 1)

    def create(self, broker, provider):
        p = product(provider)
        return broker.intents.create(self.store.create_event(p, "PRODUCT_CHANGED"), p)

    def test_provider_namespaces_and_new_provider_submit_permanently_refused(self):
        broker = EdgeBroker(self.store)
        identities = []
        for provider in TARGETS:
            intent = self.create(broker, provider)
            identities.append(intent["intent_id"])
            broker.intents.set_cart(intent["intent_id"], "configuration_0")
            broker.intents.mark_checkout_ready(intent["intent_id"])
            if provider != "bandwagon":
                self.assertFalse(broker.intents.mark_submitting(intent["intent_id"]))
                self.assertIsNone(broker.intents.get(intent["intent_id"])["submit_started_at"])
        self.assertEqual(len(set(identities)), 5)
        self.assertEqual(broker.intents.active_for_product("87", "dmit")["provider"], "dmit")

    def test_provider_event_and_product_url_binding_and_completed_provider_switch(self):
        broker = EdgeBroker(self.store)
        broker.handle_event(make_message("EDGE_READY", payload={"version": "0.2.1"}))
        first, other = self.create(broker, "bandwagon"), self.create(broker, "dmit")
        command = broker.enqueue("START_DRY_RUN", first["intent_id"], "87", product_payload(product()))
        broker.next_command()
        with self.assertRaisesRegex(ProtocolError, "ANOTHER_EDGE_INTENT_ACTIVE"):
            broker.enqueue("START_DRY_RUN", other["intent_id"], "87", product_payload(product("dmit")))
        for kind in ("PAGE_OPENED", "PRODUCT_VERIFIED", "CART_READY", "CHECKOUT_READY"):
            payload = {"challenge": "NONE", "login": "VALID"}
            if kind == "PAGE_OPENED":
                payload["url"] = product().product_url
            if kind == "CART_READY":
                payload["cart_id"] = "configuration_0"
            broker.handle_event(make_message(kind, intent_id=first["intent_id"], product_id="87", command_id=command["command_id"], payload=payload))
        forged = product_payload(product("dmit"))
        forged["product"]["url"] += "?gid=8"
        with self.assertRaisesRegex(ProtocolError, "INTENT_PRODUCT_CHANGED"):
            broker.enqueue("START_DRY_RUN", other["intent_id"], "87", forged)
        command = broker.enqueue("START_DRY_RUN", other["intent_id"], "87", product_payload(product("dmit")))
        self.assertEqual(command["provider"], "dmit")
        broker.next_command()
        forged_event = make_message("PAGE_OPENED", provider="bandwagon", intent_id=other["intent_id"], product_id="87", command_id=command["command_id"], payload={"url": product().product_url})
        with self.assertRaisesRegex(ProtocolError, "INTENT_IDENTITY_REJECTED"):
            broker.handle_event(forged_event)
        self.assertEqual(broker.intents.active_for_product("87", "bandwagon")["intent_id"], first["intent_id"])
        self.assertEqual(broker.intents.get(first["intent_id"])["state"], "CHECKOUT_READY")

    def test_only_acknowledged_read_only_cancel_releases_executor_and_product(self):
        broker = EdgeBroker(self.store)
        broker.handle_event(make_message("EDGE_READY", payload={"version": "0.2.1"}))
        intent = self.create(broker, "vps")
        opened = broker.enqueue("OPEN_PRODUCT", intent["intent_id"], "87", product_payload(product("vps")))
        broker.next_command()
        broker.handle_event(make_message("PAGE_OPENED", provider="vps", intent_id=intent["intent_id"], product_id="87",
            command_id=opened["command_id"], payload={"url": product("vps").product_url, "challenge": "NONE"}))
        cancelled = broker.enqueue("CANCEL_INTENT", intent["intent_id"], "87")
        self.assertEqual(broker.status()["current_intent"], intent["intent_id"])
        self.assertIsNotNone(broker.intents.active_for_product("87", "vps"))
        broker.next_command()
        response = broker.handle_event(make_message("FAILED", provider="vps", intent_id=intent["intent_id"], product_id="87",
            command_id=cancelled["command_id"], payload={"code": "CANCELLED", "stage": "CANCELLED"}))
        self.assertTrue(response["read_only_occupancy_released"])
        self.assertEqual(broker.intents.get(intent["intent_id"])["state"], "PRE_SUBMIT_ABORTED")
        self.assertEqual(broker.intents.get(intent["intent_id"])["edge_state"], "CANCELLED")
        self.assertIsNone(broker.status()["current_intent"])
        self.assertIsNone(broker.intents.active_for_product("87", "vps"))
        self.assertTrue(broker.can_start_provider("dmit"))
        replacement = self.create(broker, "vps")
        self.assertNotEqual(replacement["intent_id"], intent["intent_id"])
        self.assertEqual(len(broker.intents.list()), 2)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM edge_commands WHERE intent_id=?", (intent["intent_id"],)).fetchone()[0], 2)

    def test_cancelled_write_history_cart_or_uncertainty_never_releases_lock(self):
        cases = [("START_DRY_RUN", None, None), ("OPEN_PRODUCT", "cart_identifier", "configuration_0"),
                 ("OPEN_PRODUCT", "edge_mutation_uncertain", 1), ("OPEN_PRODUCT", "edge_checkpoint", "PRODUCT_VERIFIED")]
        for index, (kind, field, value) in enumerate(cases):
            with self.subTest(kind=kind, field=field), Store(Path(self.temp.name) / f"refuse-{index}.sqlite3") as store:
                broker = EdgeBroker(store)
                broker.handle_event(make_message("EDGE_READY", payload={"version": "0.2.1"}))
                p = product("vps")
                intent = broker.intents.create(store.create_event(p, "PRODUCT_CHANGED"), p)
                broker.enqueue(kind, intent["intent_id"], "87", product_payload(p))
                broker.next_command()
                if field:
                    store.connection.execute(f"UPDATE purchase_intents SET {field}=? WHERE intent_id=?", (value, intent["intent_id"]))
                cancelled = broker.enqueue("CANCEL_INTENT", intent["intent_id"], "87")
                broker.next_command()
                response = broker.handle_event(make_message("FAILED", provider="vps", intent_id=intent["intent_id"], product_id="87",
                    command_id=cancelled["command_id"], payload={"code": "CANCELLED"}))
                self.assertFalse(response["read_only_occupancy_released"])
                self.assertEqual(broker.intents.get(intent["intent_id"])["state"], "INTENT_CREATED")
                self.assertEqual(broker.status()["current_intent"], intent["intent_id"])
                self.assertIsNotNone(broker.intents.active_for_product("87", "vps"))

    def test_cancel_receipt_with_unexpected_cart_evidence_cannot_be_erased_by_recancel(self):
        broker = EdgeBroker(self.store)
        broker.handle_event(make_message("EDGE_READY", payload={"version": "0.2.1"}))
        intent = self.create(broker, "vps")
        broker.enqueue("OPEN_PRODUCT", intent["intent_id"], "87", product_payload(product("vps")))
        broker.next_command()
        for payload in ({"code": "CANCELLED", "cart_id": "unexpected"}, {"code": "CANCELLED"}):
            cancelled = broker.enqueue("CANCEL_INTENT", intent["intent_id"], "87")
            broker.next_command()
            broker.handle_event(make_message("FAILED", provider="vps", intent_id=intent["intent_id"], product_id="87",
                command_id=cancelled["command_id"], payload=payload))
            self.assertTrue(broker.intents.get(intent["intent_id"])["edge_mutation_uncertain"])
            self.assertIsNotNone(broker.intents.active_for_product("87", "vps"))


class MultiProviderCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_price_non_bandwagon_still_enqueues_read_only_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(root=Path(directory))
            config.prepare()
            with Store(config.root / "data/autograb.sqlite3") as store:
                store.ingest([product("dmit")])
                EdgeBroker(store).handle_event(make_message("EDGE_READY", payload={"version": "0.2.1"}))
            output = io.StringIO()
            with redirect_stdout(output):
                await run_edge(Namespace(command="edge-dry-run", provider="dmit", product_id="87", wait_seconds=0), config)
            self.assertIn("ADAPTER_READ_ONLY", output.getvalue())
            with Store(config.root / "data/autograb.sqlite3") as store:
                command = json.loads(store.connection.execute("SELECT message_json FROM edge_commands").fetchone()[0])
                self.assertEqual(command["type"], "OPEN_PRODUCT")
                self.assertEqual(command["payload"]["product"]["cents"], 9900)

    async def test_same_pid_selects_provider_and_unknown_price_only_enqueues_open(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(root=Path(directory))
            config.prepare()
            with Store(config.root / "data/autograb.sqlite3") as store:
                store.ingest([product()])
                store.ingest([product("dmit", unknown=True)])
                EdgeBroker(store).handle_event(make_message("EDGE_READY", payload={"version": "0.2.1"}))
            with patch("autograb.edge_cli.open_edge") as opened, redirect_stdout(io.StringIO()):
                await run_edge(Namespace(command="edge-dry-run", provider="dmit", product_id="87", wait_seconds=0), config)
                opened.assert_not_called()
            with Store(config.root / "data/autograb.sqlite3") as store:
                intent = IntentStore(store).list()[0]
                command = json.loads(store.connection.execute("SELECT message_json FROM edge_commands WHERE intent_id=?", (intent["intent_id"],)).fetchone()[0])
                self.assertEqual(intent["provider"], "dmit")
                self.assertEqual(command["type"], "OPEN_PRODUCT")
                self.assertEqual(command["payload"]["product"]["cents"], None)
                self.assertIsNone(intent["submit_started_at"])


if __name__ == "__main__":
    unittest.main()
