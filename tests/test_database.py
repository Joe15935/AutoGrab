"""Storage tests exercise persisted transitions and independent connections."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import unittest

from autograb.core.models import Product
from autograb.storage.database import Store


def product(product_id="1", availability="SOLD_OUT", **changes):
    values = dict(product_id=product_id, name="Sample VPS", availability=availability,
                  prices=[{"amount": 99, "period": "year"}],
                  product_url=f"https://example.test/products/{product_id}", eligible=True)
    values.update(changes)
    return Product(**values)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_database_and_sidecar_symlinks_leave_external_file_unchanged(self):
        outside = Path(self.directory.name) / "outside-user-file.txt"
        outside.write_bytes(b"User file must remain unchanged.\n")
        outside.chmod(0o644)
        before = (outside.read_bytes(), outside.stat().st_mode, outside.stat().st_mtime_ns)
        for index, suffix in enumerate(("", "-wal", "-shm", "-journal")):
            with self.subTest(suffix=suffix):
                project = Path(self.directory.name) / f"symlink-case-{index}"
                project.mkdir()
                database = project / "state.sqlite3"
                redirected = Path(str(database) + suffix)
                redirected.symlink_to(outside)
                with self.assertRaises(ValueError):
                    Store(database)
                self.assertTrue(redirected.is_symlink())
                self.assertEqual((outside.read_bytes(), outside.stat().st_mode, outside.stat().st_mtime_ns), before)
                if suffix:
                    self.assertFalse(database.exists())

    def test_database_and_sidecar_hardlinks_leave_external_file_unchanged(self):
        outside = Path(self.directory.name) / "outside-hardlink-file.txt"
        outside.write_bytes(b"Hard-linked user content.\n")
        outside.chmod(0o644)
        before = (outside.read_bytes(), outside.stat().st_mode, outside.stat().st_mtime_ns)
        for index, suffix in enumerate(("", "-wal", "-shm", "-journal")):
            with self.subTest(suffix=suffix):
                project = Path(self.directory.name) / f"hardlink-case-{index}"
                project.mkdir()
                database = project / "state.sqlite3"
                linked = Path(str(database) + suffix)
                os.link(outside, linked)
                try:
                    with self.assertRaises(ValueError):
                        Store(database)
                    self.assertEqual((outside.read_bytes(), outside.stat().st_mode, outside.stat().st_mtime_ns), before)
                    if suffix:
                        self.assertFalse(database.exists())
                finally:
                    linked.unlink()

    def test_database_and_sidecars_reject_nonregular_files_without_opening(self):
        for kind in ("directory", "fifo"):
            for index, suffix in enumerate(("", "-wal", "-shm", "-journal")):
                with self.subTest(kind=kind, suffix=suffix):
                    project = Path(self.directory.name) / f"nonregular-{kind}-{index}"
                    project.mkdir()
                    database = project / "state.sqlite3"
                    invalid = Path(str(database) + suffix)
                    if kind == "directory":
                        invalid.mkdir()
                    else:
                        os.mkfifo(invalid)
                    with self.assertRaises(ValueError):
                        Store(database)

    def test_new_and_existing_database_files_are_private_without_data_loss(self):
        self.store.ingest([product()])
        managed = [Path(str(self.path) + suffix) for suffix in ("", "-wal", "-shm")]
        for local_file in managed:
            self.assertTrue(local_file.exists())
            self.assertEqual(stat.S_IMODE(local_file.stat().st_mode), 0o600)
            local_file.chmod(0o666)
        with Store(self.path) as reopened:
            self.assertEqual(reopened.list_products(), [product()])
            for local_file in managed:
                self.assertEqual(stat.S_IMODE(local_file.stat().st_mode), 0o600)
        self.assertEqual(self.store.list_products(), [product()])

    def test_first_catalogue_is_baseline_even_when_available_and_restart_keeps_it(self):
        result = self.store.ingest([product(availability="AVAILABLE")])
        self.assertTrue(result["baseline_initialized"])
        self.assertEqual(result["events"], [])
        self.store.close()
        self.store = Store(self.path)
        result = self.store.ingest([product(availability="AVAILABLE")])
        self.assertFalse(result["baseline_initialized"])
        self.assertEqual(result["events"], [])
        self.assertEqual(self.store.summary()["known_count"], 1)

    def test_fifty_available_scans_deduplicate_and_second_edge_is_new_generation(self):
        self.store.ingest([product()])
        first = self.store.ingest([product(availability="AVAILABLE")])["events"]
        self.assertEqual([event["event_type"] for event in first], ["RESTOCK"])
        for _ in range(50):
            self.assertEqual(self.store.ingest([product(availability="AVAILABLE")])["events"], [])
        self.assertEqual(self.store.ingest([product()])["events"], [])
        second = self.store.ingest([product(availability="AVAILABLE")])["events"][0]
        self.assertNotEqual(first[0]["id"], second["id"])
        self.assertEqual(first[0]["generation"], 1)
        self.assertEqual(second["generation"], 2)
        self.assertEqual(self.store.summary()["event_count"], 2)

    def test_new_product_and_metadata_change(self):
        self.store.ingest([product()])
        result = self.store.ingest([product(), product("2", availability="AVAILABLE")])
        self.assertEqual(result["events"][0]["event_type"], "NEW_PRODUCT")
        changed = product("2", availability="AVAILABLE", prices=[{"amount": 100, "period": "year"}])
        result = self.store.ingest([product(), changed])
        self.assertEqual(result["events"][0]["event_type"], "PRODUCT_CHANGED")
        self.assertEqual(self.store.ingest([product(), changed])["events"], [])

    def test_invalid_partial_and_empty_scans_do_not_initialize_baseline(self):
        for catalogue, options in [([], {}), ([product()], {"complete": False}),
                                   ([product(), product("2", name="")], {}),
                                   ([product(), product()], {}),
                                   ([product(availability="ERROR")], {})]:
            with self.subTest(catalogue=catalogue, options=options):
                with self.assertRaises(ValueError):
                    self.store.ingest(catalogue, **options)
                self.assertFalse(self.store.summary()["baseline_initialized"])
                self.assertEqual(self.store.list_products(), [])

    def test_invalid_scan_rolls_back_without_changing_existing_state(self):
        self.store.ingest([product()])
        before = self.store.summary()
        with self.assertRaises(ValueError):
            self.store.ingest([product(availability="AVAILABLE"), product("2", name="")])
        self.assertEqual(self.store.list_products(), [product()])
        self.assertEqual(self.store.summary(), before)
        self.assertEqual(self.store.ingest([product(availability="AVAILABLE")])["events"][0]["event_type"], "RESTOCK")

    def test_mid_transaction_database_failure_rolls_back_products_and_events(self):
        self.store.ingest([product()])
        before = self.store.summary()
        self.store.connection.executescript("""
            CREATE TRIGGER reject_fixture BEFORE INSERT ON products
            WHEN NEW.product_id = '2'
            BEGIN SELECT RAISE(ABORT, 'fixture transaction failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest([product(availability="AVAILABLE"), product("2")])
        self.assertEqual(self.store.list_products(), [product()])
        self.assertEqual(self.store.summary(), before)
        self.assertEqual(self.store.list_events(), [])
        self.store.connection.execute("DROP TRIGGER reject_fixture")
        self.assertEqual(len(self.store.ingest([product(availability="AVAILABLE")])["events"]), 1)

    def test_missing_products_and_unknown_stock_never_create_false_stock_edges(self):
        available = product(availability="AVAILABLE")
        self.store.ingest([available, product("2")])
        self.store.ingest([product("2")])
        self.assertEqual(self.store.list_products()[0].availability, "AVAILABLE")
        self.assertEqual(self.store.ingest([replace(available, availability="UNKNOWN"), product("2")])["events"], [])
        self.assertEqual(self.store.ingest([available, product("2")])["events"], [])
        self.store.ingest([product(), product("2")])
        self.store.ingest([product(availability="UNKNOWN"), product("2")])
        events = self.store.ingest([available, product("2")])["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "RESTOCK")

    def test_simulated_event_does_not_initialize_or_mutate_real_baseline(self):
        simulation = self.store.create_event(product(availability="AVAILABLE"), "RESTOCK", simulated=True, dedupe_key="simulation:one")
        self.assertTrue(simulation["simulated"])
        self.assertFalse(self.store.summary()["baseline_initialized"])
        self.assertEqual(self.store.list_products(), [])
        self.store.ingest([product()])
        self.store.create_event(product(availability="AVAILABLE"), "RESTOCK", simulated=True)
        self.assertEqual(self.store.list_products(), [product()])
        real = self.store.ingest([product(availability="AVAILABLE")])["events"][0]
        self.assertEqual(real["generation"], 1)
        self.assertFalse(real["simulated"])
        self.assertEqual(self.store.create_event(product(availability="AVAILABLE"), "RESTOCK", simulated=True, dedupe_key="simulation:one")["id"], simulation["id"])
        with self.assertRaises(ValueError):
            self.store.create_event(product("2"), "RESTOCK", simulated=True, dedupe_key="simulation:one")

    def test_simulated_event_type_requires_simulated_flag(self):
        for contradictory in (False, None, "true", 1):
            with self.subTest(simulated=contradictory), self.assertRaises(ValueError):
                self.store.create_event(product(), "SIMULATED", simulated=contradictory)
        self.assertEqual(self.store.list_events(), [])
        event = self.store.create_event(product(), "SIMULATED", simulated=True)
        self.assertEqual(event["event_type"], "SIMULATED")
        self.assertTrue(event["simulated"])
        self.assertFalse(self.store.summary()["baseline_initialized"])

    def test_concurrent_claims_have_exactly_one_winner_and_cannot_requeue(self):
        event = self.store.create_event(product(), "RESTOCK", simulated=True)
        barrier = threading.Barrier(8)
        def claim():
            with Store(self.path) as other:
                barrier.wait(timeout=10)
                return other.claim_event(event["id"])
        with ThreadPoolExecutor(max_workers=8) as pool:
            winners = list(pool.map(lambda _: claim(), range(8)))
        self.assertEqual(sum(winners), 1)
        self.assertEqual(self.store.get_event(event["id"])["status"], "RUNNING")
        self.store.update_event(event["id"], "HOLD", {"duration_ms": 10})
        self.store.update_event(event["id"], "HOLD", {"reason": "manual_review"})
        self.assertEqual(self.store.get_event(event["id"])["details"], {"duration_ms": 10, "reason": "manual_review"})
        self.assertFalse(self.store.claim_event(event["id"]))
        with self.assertRaises(ValueError):
            self.store.update_event(event["id"], "PENDING")

    def test_concurrent_baselines_and_restock_scans_commit_once(self):
        def ingest(availability):
            with Store(self.path) as other:
                return other.ingest([product(availability=availability)])
        with ThreadPoolExecutor(max_workers=4) as pool:
            baselines = list(pool.map(ingest, ["SOLD_OUT"] * 4))
            restocks = list(pool.map(ingest, ["AVAILABLE"] * 4))
        self.assertEqual(sum(scan["baseline_initialized"] for scan in baselines), 1)
        self.assertEqual(sum(len(scan["events"]) for scan in baselines), 0)
        self.assertEqual(sum(len(scan["events"]) for scan in restocks), 1)
        self.assertEqual(self.store.summary()["event_count"], 1)

    def test_restart_recovery_does_not_retry_uncertain_event(self):
        running = self.store.create_event(product(), "RESTOCK", simulated=True)
        pending = self.store.create_event(product("2"), "RESTOCK", simulated=True)
        self.store.claim_event(running["id"])
        self.store.start_run("dry-run")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.recover_interrupted(), 1)
        self.assertEqual(self.store.get_event(running["id"])["status"], "INTERRUPTED")
        self.assertFalse(self.store.claim_event(running["id"]))
        self.assertEqual(self.store.get_event(pending["id"])["status"], "PENDING")
        self.assertEqual(self.store.recover_interrupted(), 0)
        self.assertEqual(self.store.connection.execute("SELECT status FROM runs").fetchone()[0], "INTERRUPTED")

    def test_recovery_interrupts_every_inflight_progress_but_preserves_terminal(self):
        progress = ("RUNNING", "DETECTED", "VERIFYING", "PRODUCT_VERIFIED", "OPENING_BROWSER",
                    "CART_READY", "DRY_RUN_BOUNDARY_REACHED", "NOTIFYING")
        preserved = ("PENDING", "COMPLETE", "FAILED", "CAPTCHA_REQUIRED", "LOGIN_REQUIRED")
        event_ids = {}
        for status in (*progress, *preserved):
            event = self.store.create_event(product(status), "SIMULATED", simulated=True)
            event_ids[status] = event["id"]
            if status != "PENDING":
                self.assertTrue(self.store.claim_event(event["id"]))
                if status != "RUNNING":
                    self.store.update_event(event["id"], status, {"last_step": status})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.recover_interrupted(), len(progress))
        for original, event_id in event_ids.items():
            with self.subTest(original=original):
                expected = "INTERRUPTED" if original in progress else original
                actual = self.store.get_event(event_id)
                self.assertEqual(actual["status"], expected)
                if original not in ("PENDING", "RUNNING"):
                    self.assertEqual(actual["details"], {"last_step": original})
                if original != "PENDING":
                    self.assertFalse(self.store.claim_event(event_id))
        self.assertEqual(self.store.recover_interrupted(), 0)

    def test_notification_and_run_history_survive_reopen(self):
        event = self.store.create_event(product(), "NEW_PRODUCT", simulated=True)
        notification = self.store.record_notification(event["id"], "SENT", "SMTP accepted")
        run = self.store.start_run("fixture")
        self.store.finish_run(run, "PASS", {"events": 1})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.summary()["notification_count"], 1)
        self.assertEqual(self.store.summary()["run_count"], 1)
        self.assertEqual(self.store.connection.execute("SELECT id FROM notifications").fetchone()[0], notification)
        self.assertEqual(self.store.connection.execute("SELECT status FROM runs").fetchone()[0], "PASS")
        with self.assertRaises(KeyError):
            self.store.finish_run("missing", "PASS")

    def test_json_serialization_failure_cannot_partially_commit_event_update(self):
        event = self.store.create_event(product(), "NEW_PRODUCT", simulated=True)
        with self.assertRaises(ValueError):
            self.store.update_event(event["id"], "HOLD", {"duration_ms": float("nan")})
        self.assertEqual(self.store.get_event(event["id"])["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
