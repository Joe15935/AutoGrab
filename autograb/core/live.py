"""Ephemeral LIVE permission and independently writable emergency signals.

The guard never restores authority from disk. A process must explicitly choose
LIVE, obtain fresh preflight evidence, and arm a bounded lease. Call
``assert_can_submit`` again immediately before dispatch, after persisting the
purchase intent's ORDER_SUBMITTING marker. This module performs no network I/O.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import time
from types import MappingProxyType
from typing import Mapping

from .errors import AutoGrabError


REQUIRED_CHECKS = frozenset({"database", "browser", "site", "session", "baseline", "email", "boundary"})
SIGNALS = {"disarm": "disarm.signal", "stop_monitoring": "stop-monitoring.signal"}
MAX_ARM_DURATION = timedelta(hours=24)
PREFLIGHT_MAX_AGE = timedelta(seconds=60)
SMTP_PROOF_MAX_AGE = timedelta(hours=24)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True)
class SMTPProof:
    """Evidence supplied only after an actual SMTP transaction was accepted.

    Configured credentials, mocked sends, and connection-only checks are not
    acceptance evidence. The caller is responsible for measuring the real send;
    the explicit source prevents accidentally treating fixtures as preflight.
    """

    status: str
    source: str
    accepted_at: datetime


@dataclass(frozen=True)
class Preflight:
    checks: Mapping[str, str]
    smtp_proof: SMTPProof | None = None
    checked_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self):
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))

    def failures(self, now: datetime) -> tuple[str, ...]:
        failed = sorted(name for name in REQUIRED_CHECKS if self.checks.get(name) != "PASS")
        if not _aware(self.checked_at) or not timedelta(0) <= now - self.checked_at <= PREFLIGHT_MAX_AGE:
            failed.append("preflight_freshness")
        proof = self.smtp_proof
        if (
            not isinstance(proof, SMTPProof)
            or proof.status != "SMTP_ACCEPTED"
            or proof.source != "REAL_SMTP"
            or not _aware(proof.accepted_at)
            or not timedelta(0) <= now - proof.accepted_at <= SMTP_PROOF_MAX_AGE
        ):
            failed.append("email_real_smtp")
        return tuple(failed)

    def safe_status(self, now: datetime | None = None) -> dict:
        failed = self.failures(now or _utc_now())
        return {
            "checks": {name: "PASS" if self.checks.get(name) == "PASS" else "NOT_READY" for name in sorted(REQUIRED_CHECKS)},
            "ready": not failed,
            "failed_checks": list(failed),
            "email_proof": "REAL_SMTP_ACCEPTED" if "email_real_smtp" not in failed else "NOT_VERIFIED",
        }


def _signal_path(data_dir: Path, kind: str) -> Path:
    if kind not in SIGNALS:
        raise ValueError("Unknown control signal")
    return Path(data_dir) / SIGNALS[kind]


def signal_present(data_dir: Path, kind: str) -> bool:
    """Any directory entry at the signal path stops action, including a symlink."""
    try:
        _signal_path(data_dir, kind).lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise AutoGrabError("CONTROL_SIGNAL_UNREADABLE") from None


def _open_data_dir(data_dir: Path) -> int:
    directory = Path(data_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise AutoGrabError("UNSAFE_CONTROL_DIRECTORY") from None


def _write_signal(data_dir: Path, kind: str) -> None:
    """Does not acquire the controller lock: DISARM works while it is running."""
    filename = _signal_path(data_dir, kind).name
    directory_fd = _open_data_dir(data_dir)
    fd = None
    try:
        fd = os.open(filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AutoGrabError("UNSAFE_CONTROL_FILE")
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        body = json.dumps({"version": 1, "signal": kind, "at": _utc_now().isoformat()}).encode()
        os.write(fd, body)
        os.fsync(fd)
        os.fsync(directory_fd)
    except OSError:
        raise AutoGrabError("CONTROL_SIGNAL_WRITE_FAILED") from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory_fd)


def signal_disarm(data_dir: Path) -> None:
    _write_signal(data_dir, "disarm")


def signal_stop_monitoring(data_dir: Path) -> None:
    _write_signal(data_dir, "stop_monitoring")


def clear_signal(data_dir: Path, kind: str, *, explicit: bool = False) -> None:
    """Only an explicit user command clears a persistent emergency signal."""
    if explicit is not True:
        raise AutoGrabError("EXPLICIT_SIGNAL_CLEAR_REQUIRED")
    filename = _signal_path(data_dir, kind).name
    directory_fd = _open_data_dir(data_dir)
    try:
        try:
            metadata = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AutoGrabError("UNSAFE_CONTROL_FILE")
        os.unlink(filename, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError:
        raise AutoGrabError("CONTROL_SIGNAL_CLEAR_FAILED") from None
    finally:
        os.close(directory_fd)


class LiveGuard:
    def __init__(self, data_dir: Path, *, mode: str = "DRY_RUN", provider: str = "bandwagon",
                 clock=time.monotonic_ns, utc_clock=_utc_now):
        if mode not in {"DRY_RUN", "LIVE"}:
            raise ValueError("Unknown runtime mode")
        if provider != "bandwagon":
            raise ValueError("Only BandwagonHost is supported")
        self.data_dir, self._mode, self.provider = Path(data_dir), mode, provider
        self.clock, self.utc_clock = clock, utc_clock
        self._pid = os.getpid()
        self._armed_until: datetime | None = None
        self._deadline_ns: int | None = None
        self._arm_ns: int | None = None
        self._reason = "NOT_ARMED"

    @property
    def mode(self) -> str:
        return self._mode

    def disarm(self, reason: str = "MANUAL_DISARM") -> None:
        self._armed_until = self._deadline_ns = self._arm_ns = None
        # This diagnostic can be logged; never accept arbitrary user text.
        self._reason = reason if reason in {
            "MANUAL_DISARM", "ARM_EXPIRED", "KILL_SWITCH", "PROCESS_CHANGED",
            "CLOCK_INVALID", "PREFLIGHT_FAILED", "CONTROL_SIGNAL_UNREADABLE",
        } else "MANUAL_DISARM"

    def _refresh(self) -> None:
        if os.getpid() != self._pid:
            self.disarm("PROCESS_CHANGED")
            return
        try:
            if signal_present(self.data_dir, "disarm"):
                self.disarm("KILL_SWITCH")
                return
        except AutoGrabError:
            self.disarm("CONTROL_SIGNAL_UNREADABLE")
            return
        if self._armed_until is None:
            return
        now_ns, now_utc = self.clock(), self.utc_clock()
        if not _aware(now_utc) or now_ns < self._arm_ns:
            self.disarm("CLOCK_INVALID")
        elif now_ns >= self._deadline_ns or now_utc >= self._armed_until:
            self.disarm("ARM_EXPIRED")

    def arm(self, preflight: Preflight, *, duration: timedelta | None = None,
            armed_until: datetime | None = None) -> dict:
        # A rejected attempt also revokes any earlier lease.
        self.disarm()
        if self.mode != "LIVE":
            raise AutoGrabError("DRY_RUN_CANNOT_ARM")
        if os.getpid() != self._pid:
            raise AutoGrabError("PROCESS_CHANGED")
        if signal_present(self.data_dir, "disarm"):
            raise AutoGrabError("KILL_SWITCH_ACTIVE")
        now = self.utc_clock()
        if not _aware(now):
            raise AutoGrabError("CLOCK_INVALID")
        if not isinstance(preflight, Preflight) or preflight.failures(now):
            self.disarm("PREFLIGHT_FAILED")
            raise AutoGrabError("LIVE_PREFLIGHT_FAILED")
        if armed_until is not None and duration is not None:
            raise ValueError("Choose arm duration or expiry, not both")
        if armed_until is not None:
            if not _aware(armed_until):
                raise ValueError("ARM expiry must include a timezone")
            duration = armed_until - now
        else:
            duration = timedelta(hours=1) if duration is None else duration
        if not isinstance(duration, timedelta) or not timedelta(0) < duration <= MAX_ARM_DURATION:
            raise ValueError("ARM duration must be greater than zero and at most 24 hours")
        self._arm_ns = self.clock()
        self._deadline_ns = self._arm_ns + int(duration.total_seconds() * 1_000_000_000)
        self._armed_until = (now + duration).astimezone(timezone.utc)
        self._reason = None
        return self.status()

    def assert_can_submit(self, preflight: Preflight) -> None:
        """Last local check before an order-creation request, never a payment."""
        self._refresh()
        if self.mode != "LIVE":
            raise AutoGrabError("DRY_RUN_CANNOT_SUBMIT")
        if self._armed_until is None:
            raise AutoGrabError("LIVE_NOT_ARMED")
        if not isinstance(preflight, Preflight) or preflight.failures(self.utc_clock()):
            self.disarm("PREFLIGHT_FAILED")
            raise AutoGrabError("LIVE_PREFLIGHT_FAILED")

    def status(self) -> dict:
        self._refresh()
        armed = self._armed_until is not None
        return {
            "mode": self.mode,
            "provider": self.provider,
            "armed": armed,
            "armed_until": self._armed_until.isoformat(timespec="seconds") if armed else None,
            "remaining_seconds": max(0, (self._deadline_ns - self.clock()) // 1_000_000_000) if armed else 0,
            "reason": self._reason,
            "order_creation": "LIVE_ORDER_CREATION_ENABLED" if armed else "DISABLED",
            "automatic_payment": "DISABLED",
        }
