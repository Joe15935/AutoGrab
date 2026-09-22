"""No website or mail server: test permission gates and durable stop signals."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from autograb.core.errors import AutoGrabError
from autograb.core.live import (
    LiveGuard, Preflight, REQUIRED_CHECKS, SMTPProof, clear_signal,
    signal_disarm, signal_present, signal_stop_monitoring,
)
from autograb.core.lock import ProcessLock


class FakeClock:
    def __init__(self):
        self.ns = 1_000_000_000
        self.utc = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.ns += int(seconds * 1_000_000_000)
        self.utc += timedelta(seconds=seconds)


class LiveGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name) / "data"
        self.clock = FakeClock()

    def guard(self, mode="LIVE"):
        return LiveGuard(self.data, mode=mode, clock=lambda: self.clock.ns, utc_clock=lambda: self.clock.utc)

    def ready(self, **overrides):
        # Explicitly constructed evidence is a fixture, never a claimed real send.
        return Preflight(
            checks={name: overrides.get(name, "PASS") for name in REQUIRED_CHECKS},
            smtp_proof=SMTPProof("SMTP_ACCEPTED", "REAL_SMTP", self.clock.utc),
            checked_at=self.clock.utc,
        )

    def assert_code(self, code, action):
        with self.assertRaises(AutoGrabError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_default_mode_and_armed_state_are_safe(self):
        guard = LiveGuard(self.data)
        self.assertEqual(guard.status()["mode"], "DRY_RUN")
        self.assertFalse(guard.status()["armed"])
        self.assertEqual(guard.status()["automatic_payment"], "DISABLED")

    def test_dry_run_cannot_arm(self):
        self.assert_code("DRY_RUN_CANNOT_ARM", lambda: self.guard("DRY_RUN").arm(self.ready()))

    def test_dry_run_cannot_submit(self):
        self.assert_code("DRY_RUN_CANNOT_SUBMIT", lambda: self.guard("DRY_RUN").assert_can_submit(self.ready()))

    def test_live_disarmed_cannot_submit(self):
        self.assert_code("LIVE_NOT_ARMED", lambda: self.guard().assert_can_submit(self.ready()))

    def test_armed_live_reaches_submit_gate(self):
        guard = self.guard()
        status = guard.arm(self.ready())
        guard.assert_can_submit(self.ready())
        self.assertTrue(status["armed"])
        self.assertEqual(status["remaining_seconds"], 3600)
        self.assertEqual(status["order_creation"], "LIVE_ORDER_CREATION_ENABLED")

    def test_missing_email_refuses_arm(self):
        guard = self.guard()
        self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: guard.arm(self.ready(email="NOT_CONFIGURED")))
        self.assertFalse(guard.status()["armed"])

    def test_configuration_without_smtp_acceptance_is_insufficient(self):
        self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(replace(self.ready(), smtp_proof=None)))

    def test_mock_or_connection_only_proof_cannot_arm(self):
        for status, source in (("SMTP_ACCEPTED", "MOCK"), ("CONNECTED", "REAL_SMTP"), ("SMTP_ACCEPTED", "SIMULATED")):
            with self.subTest(status=status, source=source):
                preflight = replace(self.ready(), smtp_proof=SMTPProof(status, source, self.clock.utc))
                self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(preflight))

    def test_all_critical_checks_are_required(self):
        for name in REQUIRED_CHECKS:
            with self.subTest(name=name):
                self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(self.ready(**{name: "UNKNOWN"})))

    def test_boolean_checks_are_not_accepted_as_pass(self):
        preflight = replace(self.ready(), checks={name: True for name in REQUIRED_CHECKS})
        self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(preflight))

    def test_preflight_cannot_be_mutated_after_creation(self):
        checks = {name: "PASS" for name in REQUIRED_CHECKS}
        preflight = replace(self.ready(), checks=checks)
        checks["session"] = "FAIL"
        self.assertEqual(preflight.checks["session"], "PASS")
        with self.assertRaises(TypeError):
            preflight.checks["session"] = "FAIL"

    def test_stale_preflight_cannot_arm(self):
        old = replace(self.ready(), checked_at=self.clock.utc - timedelta(seconds=61))
        self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(old))

    def test_future_preflight_or_smtp_proof_cannot_arm(self):
        future = self.clock.utc + timedelta(seconds=1)
        for preflight in (replace(self.ready(), checked_at=future),
                          replace(self.ready(), smtp_proof=SMTPProof("SMTP_ACCEPTED", "REAL_SMTP", future))):
            with self.subTest(preflight=preflight):
                self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(preflight))

    def test_old_smtp_proof_cannot_arm(self):
        proof = SMTPProof("SMTP_ACCEPTED", "REAL_SMTP", self.clock.utc - timedelta(hours=25))
        self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(replace(self.ready(), smtp_proof=proof)))

    def test_naive_timestamps_fail_closed(self):
        for preflight in (replace(self.ready(), checked_at=datetime(2026, 9, 22)),
                          replace(self.ready(), smtp_proof=SMTPProof("SMTP_ACCEPTED", "REAL_SMTP", datetime(2026, 9, 22)))):
            with self.subTest(preflight=preflight):
                self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: self.guard().arm(preflight))

    def test_expiry_accepts_one_hour_six_hours_or_twenty_four_hours(self):
        for hours in (1, 6, 24):
            with self.subTest(hours=hours):
                guard = self.guard()
                guard.arm(self.ready(), duration=timedelta(hours=hours))
                self.assertEqual(guard.status()["remaining_seconds"], hours * 3600)

    def test_explicit_utc_expiry(self):
        until = self.clock.utc + timedelta(minutes=15)
        guard = self.guard()
        status = guard.arm(self.ready(), armed_until=until)
        self.assertEqual(status["armed_until"], until.isoformat(timespec="seconds"))

    def test_invalid_expiry_always_leaves_guard_disarmed(self):
        for duration in (timedelta(0), timedelta(seconds=-1), timedelta(hours=25), True, 3600):
            with self.subTest(duration=duration):
                guard = self.guard()
                guard.arm(self.ready())
                with self.assertRaises(ValueError):
                    guard.arm(self.ready(), duration=duration)
                self.assertFalse(guard.status()["armed"])

    def test_naive_or_past_expiry_rejected(self):
        for until in (datetime(2026, 9, 22), self.clock.utc - timedelta(seconds=1)):
            with self.subTest(until=until):
                with self.assertRaises(ValueError):
                    self.guard().arm(self.ready(), armed_until=until)

    def test_duration_and_expiry_together_rejected(self):
        with self.assertRaises(ValueError):
            self.guard().arm(self.ready(), duration=timedelta(hours=1), armed_until=self.clock.utc + timedelta(hours=1))

    def test_expired_lease_cannot_submit(self):
        guard = self.guard()
        guard.arm(self.ready(), duration=timedelta(seconds=10))
        self.clock.advance(10)
        self.assert_code("LIVE_NOT_ARMED", lambda: guard.assert_can_submit(self.ready()))
        self.assertEqual(guard.status()["reason"], "ARM_EXPIRED")

    def test_wall_clock_backwards_does_not_extend_lease(self):
        guard = self.guard()
        guard.arm(self.ready(), duration=timedelta(seconds=10))
        self.clock.ns += 11_000_000_000
        self.clock.utc -= timedelta(days=1)
        self.assertFalse(guard.status()["armed"])
        self.assertEqual(guard.status()["reason"], "ARM_EXPIRED")

    def test_wall_clock_forward_expires_early(self):
        guard = self.guard()
        guard.arm(self.ready(), duration=timedelta(hours=1))
        self.clock.utc += timedelta(hours=2)
        self.assertFalse(guard.status()["armed"])

    def test_monotonic_clock_backwards_fails_closed(self):
        guard = self.guard()
        guard.arm(self.ready())
        self.clock.ns -= 1
        self.assertEqual(guard.status()["reason"], "CLOCK_INVALID")

    def test_submit_requires_fresh_preflight(self):
        guard, preflight = self.guard(), self.ready()
        guard.arm(preflight)
        self.clock.advance(61)
        self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: guard.assert_can_submit(preflight))
        self.assertFalse(guard.status()["armed"])

    def test_failed_session_at_dispatch_revokes_lease(self):
        guard = self.guard()
        guard.arm(self.ready())
        self.assert_code("LIVE_PREFLIGHT_FAILED", lambda: guard.assert_can_submit(self.ready(session="EXPIRED")))
        self.assertFalse(guard.status()["armed"])

    def test_restart_never_restores_arm_from_files(self):
        guard = self.guard()
        guard.arm(self.ready())
        self.data.mkdir(exist_ok=True)
        (self.data / "old-status.json").write_text('{"mode":"LIVE","armed":true}')
        fresh_process = self.guard()
        self.assertFalse(fresh_process.status()["armed"])
        self.assert_code("LIVE_NOT_ARMED", lambda: fresh_process.assert_can_submit(self.ready()))

    def test_fork_inherited_object_loses_arm(self):
        guard = self.guard()
        guard.arm(self.ready())
        with patch("autograb.core.live.os.getpid", return_value=guard._pid + 1):
            self.assertFalse(guard.status()["armed"])
            self.assertEqual(guard.status()["reason"], "PROCESS_CHANGED")
            self.assert_code("PROCESS_CHANGED", lambda: guard.arm(self.ready()))

    def test_manual_disarm_stops_submit_without_deleting_orders(self):
        guard = self.guard()
        guard.arm(self.ready())
        self.data.mkdir(exist_ok=True)
        order_file = self.data / "existing-intent.json"
        order_file.write_text('{"state":"PAYMENT_READY"}')
        guard.disarm()
        self.assert_code("LIVE_NOT_ARMED", lambda: guard.assert_can_submit(self.ready()))
        self.assertEqual(order_file.read_text(), '{"state":"PAYMENT_READY"}')

    def test_kill_switch_written_under_controller_lock_stops_next_dispatch(self):
        guard = self.guard()
        guard.arm(self.ready())
        with ProcessLock(self.data / "controller.lock"):
            signal_disarm(self.data)
        self.assert_code("LIVE_NOT_ARMED", lambda: guard.assert_can_submit(self.ready()))
        self.assertEqual(guard.status()["reason"], "KILL_SWITCH")

    def test_persistent_kill_switch_refuses_rearm_after_restart(self):
        signal_disarm(self.data)
        self.assert_code("KILL_SWITCH_ACTIVE", lambda: self.guard().arm(self.ready()))
        self.assert_code("EXPLICIT_SIGNAL_CLEAR_REQUIRED", lambda: clear_signal(self.data, "disarm"))
        clear_signal(self.data, "disarm", explicit=True)
        self.assertTrue(self.guard().arm(self.ready())["armed"])

    def test_stop_monitoring_does_not_silently_change_arm(self):
        guard = self.guard()
        guard.arm(self.ready())
        signal_stop_monitoring(self.data)
        self.assertTrue(signal_present(self.data, "stop_monitoring"))
        self.assertFalse(signal_present(self.data, "disarm"))
        self.assertTrue(guard.status()["armed"])
        clear_signal(self.data, "stop_monitoring", explicit=True)
        self.assertFalse(signal_present(self.data, "stop_monitoring"))

    def test_control_file_is_private(self):
        signal_disarm(self.data)
        self.assertEqual(stat.S_IMODE((self.data / "disarm.signal").stat().st_mode), 0o600)

    def test_symlink_kill_switch_blocks_and_cannot_overwrite_target(self):
        self.data.mkdir()
        target = Path(self.temporary.name) / "private.txt"
        target.write_text("preserve")
        (self.data / "disarm.signal").symlink_to(target)
        self.assert_code("KILL_SWITCH_ACTIVE", lambda: self.guard().arm(self.ready()))
        with self.assertRaises(AutoGrabError):
            signal_disarm(self.data)
        self.assertEqual(target.read_text(), "preserve")

    def test_hardlinked_signal_cannot_truncate_target(self):
        self.data.mkdir()
        target = Path(self.temporary.name) / "private.txt"
        target.write_text("preserve")
        (self.data / "disarm.signal").hardlink_to(target)
        self.assert_code("UNSAFE_CONTROL_FILE", lambda: signal_disarm(self.data))
        self.assertEqual(target.read_text(), "preserve")

    def test_signal_read_failure_disarms(self):
        guard = self.guard()
        guard.arm(self.ready())
        with patch("autograb.core.live.signal_present", side_effect=AutoGrabError("CONTROL_SIGNAL_UNREADABLE")):
            self.assertFalse(guard.status()["armed"])
            self.assertEqual(guard.status()["reason"], "CONTROL_SIGNAL_UNREADABLE")

    def test_unknown_mode_provider_signal_and_status_text_are_not_accepted(self):
        for kwargs in ({"mode": "unexpected"}, {"provider": "other"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                LiveGuard(self.data, **kwargs)
        with self.assertRaises(ValueError):
            signal_present(self.data, "arbitrary")
        guard = self.guard()
        guard.disarm("private-looking-text")
        self.assertNotIn("private-looking-text", str(guard.status()))

    def test_safe_preflight_status_contains_only_known_diagnostics(self):
        status = self.ready(session="private-looking-text").safe_status(self.clock.utc)
        self.assertFalse(status["ready"])
        self.assertEqual(status["checks"]["session"], "NOT_READY")
        self.assertNotIn("private-looking-text", str(status))


if __name__ == "__main__":
    unittest.main()
