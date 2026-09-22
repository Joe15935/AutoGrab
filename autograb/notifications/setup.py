"""User-operated email setup. Secrets go only to the macOS Keychain.

The private JSON file contains transport/address settings and a reference to one
AutoGrab-owned Keychain entry. It never contains a password or SMTP response.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import getpass
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any
from uuid import uuid4
import warnings

from autograb.notifications.email import EmailNotifier, KEYCHAIN_SERVICE, NotificationResult, SMTPConfig


SETTINGS_NAME = "email-settings.json"
ALLOWED_FIELDS = frozenset({"host", "port", "username", "sender", "recipient", "tls_mode",
                            "secret_service", "secret_account", "timeout"})


class EmailSetupError(ValueError):
    """Only a fixed error code is exposed to the CLI, never underlying data."""


@dataclass(frozen=True)
class SetupResult:
    status: str
    error_code: str | None = None
    notification: NotificationResult | None = None


def _open_data(root: Path, *, create: bool) -> int | None:
    data = root.resolve() / "data"
    if create:
        try:
            data.mkdir(mode=0o700)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise EmailSetupError("EMAIL_SETTINGS_PATH_UNSAFE") from None
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise EmailSetupError("EMAIL_SETTINGS_PATH_UNSAFE")
        if create:
            os.fchmod(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_settings(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - ALLOWED_FIELDS:
        raise EmailSetupError("EMAIL_SETTINGS_INVALID")
    if any(not isinstance(value.get(key, ""), str) for key in ALLOWED_FIELDS - {"port", "timeout"}):
        raise EmailSetupError("EMAIL_SETTINGS_INVALID")
    if (isinstance(value.get("port", 465), bool)
            or not isinstance(value.get("port", 465), int)
            or isinstance(value.get("timeout", 10), bool)):
        raise EmailSetupError("EMAIL_SETTINGS_INVALID")
    config = SMTPConfig.from_mapping(value, {})
    if config.error_code is not None:
        raise EmailSetupError("EMAIL_SETTINGS_INVALID")
    return value


def _read_settings(root: Path) -> dict[str, Any] | None:
    directory = _open_data(root, create=False)
    if directory is None:
        return None
    descriptor = None
    try:
        try:
            descriptor = os.open(SETTINGS_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > 16384 or info.st_mode & 0o077):
            raise EmailSetupError("EMAIL_SETTINGS_PATH_UNSAFE")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            return _validate_settings(json.load(stream))
    except EmailSetupError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise EmailSetupError("EMAIL_SETTINGS_INVALID") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def load_setup(
    root: Path, base: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
) -> SMTPConfig:
    """Saved settings override legacy nonsecret config; environment overrides both.

    Loading only reads AutoGrab's own settings file. It does not read Keychain,
    search other apps, or make a network connection.
    """
    values = dict(base or {})
    if isinstance(values.get("smtp"), Mapping):
        values = dict(values["smtp"])
    saved = _read_settings(root)
    if saved is not None:
        values.update(saved)
    return SMTPConfig.from_mapping(values, env)


def _save_settings(root: Path, settings: Mapping[str, Any], *, replace: bool) -> None:
    value = _validate_settings(dict(settings))
    directory = _open_data(root, create=True)
    if directory is None:
        raise EmailSetupError("EMAIL_SETTINGS_PATH_UNSAFE")
    temporary = f".email-settings-{uuid4().hex}.tmp"
    descriptor = None
    installed = False
    try:
        try:
            existing = os.stat(SETTINGS_NAME, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise EmailSetupError("EMAIL_SETTINGS_PATH_UNSAFE")
            if not replace:
                raise EmailSetupError("EMAIL_SETTINGS_EXISTS")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if replace:
            os.replace(temporary, SETTINGS_NAME, src_dir_fd=directory, dst_dir_fd=directory)
            installed = True
        else:
            # Atomic no-clobber installation. A concurrent setup cannot replace
            # a settings file that appeared after the initial existence check.
            os.link(temporary, SETTINGS_NAME, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            installed = True
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    except EmailSetupError:
        raise
    except FileExistsError:
        raise EmailSetupError("EMAIL_SETTINGS_EXISTS") from None
    except (OSError, ValueError, TypeError):
        raise EmailSetupError("EMAIL_SETTINGS_OUTCOME_UNKNOWN" if installed else "EMAIL_SETTINGS_SAVE_FAILED") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory)


def _keychain_backend():
    if sys.platform != "darwin":
        raise EmailSetupError("MACOS_KEYCHAIN_REQUIRED")
    try:
        import keyring
        from keyring.backends.macOS import Keyring

        backend = keyring.get_keyring()
        if type(backend) is not Keyring:
            raise EmailSetupError("UNSAFE_KEYRING_BACKEND")
        return backend
    except EmailSetupError:
        raise
    except Exception:
        raise EmailSetupError("KEYCHAIN_UNAVAILABLE") from None


async def setup_email(
    root: Path, *, input_fn: Callable[[str], str] | None = None,
    getpass_fn: Callable[[str], str] | None = None, output_fn: Callable[[str], Any] | None = None,
) -> SetupResult:
    """Interactively configure and optionally send one explicitly confirmed test.

    There is no default acceptance and no automatic test retry. Password input
    refuses getpass's echoing fallback when a real controlling terminal is absent.
    """
    ask = input_fn or input
    secret_prompt = getpass_fn or getpass.getpass
    tell = output_fn or print
    saved = False
    backend = None
    account = ""
    try:
        existing = _read_settings(root)
        replace_existing = existing is not None
        if replace_existing and ask("已有 AutoGrab 邮件设置。替换设置请输入 REPLACE，其他输入取消: ").strip() != "REPLACE":
            return SetupResult("CANCELLED")
        # Validate the supported backend before requesting a secret. Never read
        # another application or account's existing credentials.
        backend = _keychain_backend()
        tell("密码仅写入 macOS Keychain 的 AutoGrab SMTP 条目；输入不会显示。")
        host = ask("SMTP host: ").strip()
        username = ask("SMTP username: ").strip()
        sender = ask("发件邮箱（直接回车使用 SMTP username）: ").strip() or username
        recipient = ask("收件邮箱: ").strip()
        transport = ask("加密方式：输入 465 使用 SSL，或输入 587 使用 STARTTLS: ").strip()
        if transport not in {"465", "587"}:
            return SetupResult("FAILED", "SMTP_TRANSPORT_INVALID")
        account = f"autograb-{uuid4().hex}"
        settings = {
            "host": host, "username": username, "sender": sender, "recipient": recipient,
            "port": int(transport), "tls_mode": "ssl" if transport == "465" else "starttls",
            "secret_service": KEYCHAIN_SERVICE, "secret_account": account, "timeout": 10,
        }
        _validate_settings(settings)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = secret_prompt("SMTP app password（不回显）: ")
        if not isinstance(password, str) or not password or "\x00" in password:
            return SetupResult("FAILED", "SMTP_PASSWORD_REQUIRED")
        if ask("保存到 AutoGrab Keychain 和私有设置文件？输入 SAVE 确认: ").strip() != "SAVE":
            del password
            return SetupResult("CANCELLED")
        try:
            backend.set_password(KEYCHAIN_SERVICE, account, password)
        except Exception:
            raise EmailSetupError("KEYCHAIN_SAVE_FAILED") from None
        finally:
            del password
        try:
            _save_settings(root, settings, replace=replace_existing)
        except Exception as error:
            # Only this new random entry is eligible for cleanup. Existing
            # settings and their referenced credential are never deleted.
            if not isinstance(error, EmailSetupError) or str(error) != "EMAIL_SETTINGS_OUTCOME_UNKNOWN":
                try:
                    backend.delete_password(KEYCHAIN_SERVICE, account)
                except Exception:
                    pass
            raise
        saved = True
        tell("EMAIL CONFIGURED。SMTP 连接和发送尚未验证。")
        if ask("现在发送一封真实测试邮件？输入 SEND 确认，其他输入跳过: ").strip() != "SEND":
            return SetupResult("CONFIGURED")
        notification = await EmailNotifier(SMTPConfig.from_mapping(settings, {})).send_live_test()
        tell(f"Email test: {notification.status}")
        tell("SMTP ACCEPTED 仅代表服务器接受，不代表已送达收件箱。")
        return SetupResult("CONFIGURED", notification=notification)
    except (EOFError, KeyboardInterrupt):
        return SetupResult("CONFIGURED" if saved else "CANCELLED")
    except getpass.GetPassWarning:
        return SetupResult("FAILED", "SECURE_TERMINAL_REQUIRED")
    except EmailSetupError as error:
        return SetupResult("FAILED", str(error))
    except Exception:
        return SetupResult("FAILED", "EMAIL_SETUP_FAILED")
