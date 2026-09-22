"""Core safety boundaries and timing/state correctness; only temporary files."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from autograb.core.config import Config
from autograb.core.events import EventLog
from autograb.core.state import State, transition
from autograb.core.timing import Timing, utc_now


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.path = self.root / "config.toml"

    def test_defaults_and_private_runtime_directories(self):
        config = Config.load(self.root)
        self.assertEqual(config.mode, "DRY_RUN")
        self.assertEqual(config.normal_interval, 30)
        config.prepare()
        for name in ("data", "logs", "artifacts", "profiles/bandwagon"):
            with self.subTest(directory=name):
                self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o700)

    def test_live_mode_unknown_fields_and_aggressive_polling_rejected(self):
        for content in (
            'mode = "LIVE"',
            'unknown = true',
            'normal_interval = 1',
            'normal_interval = inf',
            'normal_interval = nan',
            'normal_interval = "30"',
            'timeout_ms = 1',
            'timeout_ms = true',
        ):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(ValueError):
                    Config.load(self.root, self.path)

    def test_invalid_smtp_type_and_secret_aliases_rejected(self):
        for content in (
            'smtp = []',
            'smtp = "invalid"',
            '[smtp]\npassword = "fixture-only"',
            '[smtp]\nSMTP_PASSWORD = "fixture-only"',
            '[smtp]\n_password = "fixture-only"',
        ):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(ValueError):
                    Config.load(self.root, self.path)

    def test_runtime_symlink_never_chmods_external_directory(self):
        external = Path(self.tmp.name) / "external"
        external.mkdir(mode=0o755)
        external.chmod(0o755)
        (self.root / "data").symlink_to(external, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            Config.load(self.root).prepare()
        self.assertEqual(stat.S_IMODE(external.stat().st_mode), 0o755)

    def test_profile_parent_symlink_never_creates_external_profile(self):
        external = Path(self.tmp.name) / "external-profiles"
        external.mkdir()
        (self.root / "profiles").symlink_to(external, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            Config.load(self.root).prepare()
        self.assertFalse((external / "bandwagon").exists())


class StateTests(unittest.TestCase):
    def test_complete_dry_run_path(self):
        current = State.IDLE
        for following in (
            State.DETECTED, State.VERIFYING, State.PRODUCT_VERIFIED,
            State.OPENING_BROWSER, State.CART_READY,
            State.DRY_RUN_BOUNDARY_REACHED, State.NOTIFYING, State.COMPLETE,
        ):
            current = transition(current, following)
        self.assertEqual(current, State.COMPLETE)

    def test_cannot_skip_verification_boundary_or_notification(self):
        for current, target in (
            (State.DETECTED, State.CART_READY),
            (State.OPENING_BROWSER, State.DRY_RUN_BOUNDARY_REACHED),
            (State.CART_READY, State.COMPLETE),
            (State.DRY_RUN_BOUNDARY_REACHED, State.COMPLETE),
            (State.COMPLETE, State.DETECTED),
            (State.LOGIN_REQUIRED, State.CART_READY),
            (State.CAPTCHA_REQUIRED, State.VERIFYING),
            (State.FAILED, State.DETECTED),
            (State.CART_READY, "ORDER_CREATED"),
            (State.CART_READY, "PAYMENT_READY"),
        ):
            with self.subTest(current=current, target=target):
                with self.assertRaises(ValueError):
                    transition(current, target)

    def test_human_blocks_and_failure_are_terminal(self):
        for target in (State.FAILED, State.CAPTCHA_REQUIRED, State.LOGIN_REQUIRED):
            self.assertEqual(transition(State.VERIFYING, target), target)
            with self.assertRaises(ValueError):
                transition(target, State.NOTIFYING)

    def test_baseline_returns_to_monitoring_without_detection(self):
        current = transition(State.IDLE, State.MONITORING)
        current = transition(current, State.BASELINE)
        self.assertEqual(transition(current, State.MONITORING), State.MONITORING)
        with self.assertRaises(ValueError):
            transition(State.BASELINE, State.DETECTED)


class TimingTests(unittest.TestCase):
    def test_millisecond_precision_uses_monotonic_not_wall_clock(self):
        ticks = iter((1_000_000_000, 1_111_222_000, 1_222_333_000, 1_333_444_000, 1_444_555_000, 1_555_666_000))
        timing = Timing(clock=lambda: next(ticks))
        with patch("autograb.core.timing.utc_now", side_effect=[f"2026-09-22T01:00:0{5-i}.000+00:00" for i in range(6)]):
            for stage in ("T0", "T1", "T2", "T3", "T4", "T5"):
                timing.mark(stage)
        serialized = timing.as_dict()
        self.assertEqual(serialized["durations_ms"]["detection_to_verify"], 111.222)
        self.assertEqual(serialized["durations_ms"]["detection_to_boundary"], 444.555)
        self.assertEqual(serialized["durations_ms"]["detection_to_notification"], 555.666)
        self.assertIn("SMTP_ACCEPTED", serialized["T5_definition"])

    def test_missing_marks_are_unknown_and_stage_cannot_be_rewritten(self):
        timing = Timing(clock=lambda: 123)
        timing.mark("T0")
        self.assertIsNone(timing.as_dict()["durations_ms"]["detection_to_notification"])
        with self.assertRaises(ValueError):
            timing.mark("T0")
        with self.assertRaises(ValueError):
            timing.mark("T6")

    def test_serialization_cannot_mutate_recorded_marks(self):
        timing = Timing(clock=lambda: 100)
        timing.mark("T0")
        record = timing.as_dict()
        record["marks"]["T0"]["monotonic_ns"] = 999
        self.assertEqual(timing.as_dict()["marks"]["T0"]["monotonic_ns"], 100)

    def test_utc_has_timezone_and_millisecond_precision(self):
        timestamp = utc_now()
        parsed = datetime.fromisoformat(timestamp)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(parsed))
        self.assertEqual(len(timestamp.split(".")[1].split("+")[0]), 3)


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.stdout = io.StringIO()

    def test_structured_append_and_private_permissions(self):
        with redirect_stdout(self.stdout):
            EventLog(self.path).write("BASELINE_INITIALIZATION", known_products=48, triggered=0)
            EventLog(self.path).write("RESTOCK", product_id="fixture-123", status="DETECTED")
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["mode"], "DRY_RUN")
        self.assertEqual(records[0]["provider"], "bandwagon")
        self.assertEqual(records[0]["triggered"], 0)
        self.assertEqual(records[1]["product_id"], "fixture-123")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_standard_nested_boundary_and_notification_fields_allowed(self):
        with redirect_stdout(self.stdout):
            EventLog(self.path).write(
                "EVENT_COMPLETE", notification={"status": "NOTIFICATION_FAILED", "error_code": "SMTP_AUTH_FAILED"},
                cart={"payment_url": None, "cart_url": "https://bandwagonhost.com/cart.php?a=view", "cart_reserved": False},
            )
        record = json.loads(self.path.read_text())
        self.assertIsNone(record["cart"]["payment_url"])
        self.assertEqual(record["notification"]["error_code"], "SMTP_AUTH_FAILED")

    def test_nested_secret_and_sensitive_url_rejected_before_logging(self):
        for fields in (
            {"payload": [{"Cookie": "fixture-sensitive-marker"}]},
            {"payload": {"session_token": "fixture-sensitive-marker"}},
            {"cart_url": "https://bandwagonhost.com/cart.php?a=view&token=fixture-sensitive-marker"},
            {"cart_url": "https://user:fixture-sensitive-marker@bandwagonhost.com/cart.php"},
        ):
            with self.subTest(fields=fields), redirect_stdout(self.stdout):
                with self.assertRaises(ValueError):
                    EventLog(self.path).write("FAILURE", **fields)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_reserved_context_cannot_be_overridden(self):
        with redirect_stdout(self.stdout):
            try:
                EventLog(self.path).write("RESTOCK", mode="LIVE", provider="foreign", timestamp="forged")
            except ValueError:
                return
        record = json.loads(self.path.read_text())
        self.assertEqual(record["mode"], "DRY_RUN")
        self.assertEqual(record["provider"], "bandwagon")
        self.assertNotEqual(record["timestamp"], "forged")

    def test_sensitive_fields_never_reach_file_or_stdout(self):
        with redirect_stdout(self.stdout):
            try:
                EventLog(self.path).write("FAILURE", password="fixture-sensitive-marker", payload={"cookie": "fixture-sensitive-marker"})
            except ValueError:
                pass
        self.assertNotIn("fixture-sensitive-marker", self.stdout.getvalue())
        if self.path.exists():
            self.assertNotIn("fixture-sensitive-marker", self.path.read_text())

    def test_symlink_log_cannot_overwrite_or_append_external_file(self):
        external = Path(self.tmp.name) / "external.txt"
        external.write_text("preserve")
        self.path.symlink_to(external)
        with redirect_stdout(self.stdout):
            with self.assertRaises((ValueError, OSError)):
                EventLog(self.path).write("RESTOCK", product_id="fixture-123")
        self.assertEqual(external.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
