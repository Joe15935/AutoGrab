"""One controller per project and per persistent browser profile."""
import fcntl
import os
import stat
from pathlib import Path

from .errors import AutoGrabError


def submission_lease_path(data: Path, nonce: str) -> Path:
    from uuid import UUID
    return Path(data) / 'order-leases' / (str(UUID(nonce)) + '.lock')


def lock_is_held(path: Path) -> bool:
    """Read kernel ownership, never a stale file's presence or saved PID."""
    try:
        fd = os.open(path, os.O_NOFOLLOW | os.O_RDWR)
    except OSError:
        return False
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)


class ProcessLock:
    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
        metadata = os.fstat(self.fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(self.fd)
            self.fd = None
            raise AutoGrabError("UNSAFE_LOCK_FILE")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise AutoGrabError("ALREADY_RUNNING") from None
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
