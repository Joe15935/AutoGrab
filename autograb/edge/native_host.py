"""Microsoft Edge launches this process through official Native Messaging.

stdout is exclusively length-prefixed protocol frames. The host never opens a
port, reads browser profiles, or accepts arbitrary commands/scripts/paths from
the extension. SIGTERM, EOF, malformed frames and heartbeat loss stop execution.
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import selectors
import signal
import struct
import sys
import time

from autograb.core.config import Config
from autograb.core.lock import ProcessLock
from autograb.edge.broker import EdgeBroker
from autograb.edge.protocol import MAX_FRAME_BYTES, ProtocolError, decode, encode, validate_origin
from autograb.notifications.email import EmailNotifier, NotificationResult
from autograb.notifications.setup import load_setup
from autograb.storage.database import Store


class FrameDecoder:
    """Incremental framing prevents a partial frame from blocking stop checks."""
    def __init__(self):
        self.buffer = bytearray()
        self.started = None

    def feed(self, chunk):
        if not isinstance(chunk, bytes) or len(self.buffer) + len(chunk) > 2 * MAX_FRAME_BYTES + 8:
            raise ProtocolError("FRAME_BUFFER_LIMIT")
        if chunk and not self.buffer:
            self.started = time.monotonic()
        self.buffer.extend(chunk)
        messages = []
        while len(self.buffer) >= 4:
            size = struct.unpack("=I", self.buffer[:4])[0]
            if not 0 < size <= MAX_FRAME_BYTES:
                raise ProtocolError("FRAME_SIZE_INVALID")
            if len(self.buffer) < size + 4:
                break
            raw = bytes(self.buffer[4:size + 4])
            del self.buffer[:size + 4]
            messages.append(decode(raw, direction="event"))
        if not self.buffer:
            self.started = None
        elif messages:
            self.started = time.monotonic()
        return messages

    def check_timeout(self):
        if self.started is not None and time.monotonic() - self.started > 5:
            raise ProtocolError("FRAME_TIMEOUT")


def _signal_stamp(root):
    stamps = []
    for name in ("disarm.signal", "stop-monitoring.signal"):
        try:
            info = (root / "data" / name).lstat()
            stamps.append((info.st_ino, info.st_mtime_ns, info.st_size))
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


def _send_notice(config, notice):
    try:
        notifier = EmailNotifier(load_setup(config.root, base=config.smtp))
        return asyncio.run(notifier.send_edge_human_required(notice["notice_id"], notice["status"]))
    except Exception:
        return NotificationResult("NOTIFICATION_FAILED", "EDGE_NOTICE_FAILED")


def serve(root, extension_id, origin, *, input_stream=None, output_stream=None):
    validate_origin(extension_id, origin)
    root = Path(root).resolve()
    # The command-line ID must agree with the installed project's fixed identity.
    # It cannot turn this host into an arbitrary-extension bridge.
    identity = json.loads((root / "config/edge-identity.json").read_text(encoding="utf-8"))
    if identity.get("extension_id") != extension_id:
        raise ProtocolError("EXTENSION_ID_MISMATCH")
    config = Config.load(root)
    config.prepare()
    source = input_stream or sys.stdin.buffer
    sink = output_stream or sys.stdout.buffer
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    previous = {kind: signal.signal(kind, stop) for kind in (signal.SIGTERM, signal.SIGINT)}
    try:
        with ProcessLock(root / "data/edge-native.lock"), Store(root / "data/autograb.sqlite3") as store:
            broker = EdgeBroker(store)
            # Stale connected/dispatch records from a crash never restore authority.
            broker.disconnect()
            decoder = FrameDecoder()
            initial_time = time.monotonic()
            handshake_seen = False
            last_ping = initial_time
            stamps = _signal_stamp(root)
            notices = []
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="edge-notice")
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(source, selectors.EVENT_READ)
                    while not stopping:
                        decoder.check_timeout()
                        current_stamps = _signal_stamp(root)
                        # Pre-existing disarm means LIVE OFF, not a ban on an
                        # explicit later dry run. New writes stop an active run.
                        if any(new is not None and new != old for old, new in zip(stamps, current_stamps)):
                            broker.enqueue("DISARM")
                        stamps = current_stamps
                        for future, notification_id in list(notices):
                            if future.done():
                                broker.finish_notification(notification_id, future.result())
                                notices.remove((future, notification_id))
                        for _key, _events in selector.select(timeout=0.2):
                            chunk = os.read(source.fileno(), MAX_FRAME_BYTES)
                            if not chunk:
                                if decoder.buffer:
                                    raise ProtocolError("FRAME_TRUNCATED")
                                stopping = True
                                break
                            for message in decoder.feed(chunk):
                                accepted = broker.handle_event(message)
                                notice = accepted.get("human_notification")
                                if notice:
                                    notices.append((executor.submit(_send_notice, config, notice), notice["notification_id"]))
                        if stopping:
                            break
                        status = broker.status()
                        if not status["connected"]:
                            if handshake_seen or time.monotonic() - initial_time > 15:
                                raise ProtocolError("EDGE_HEARTBEAT_LOST")
                            continue
                        handshake_seen = True
                        if time.monotonic() - last_ping >= 15:
                            broker.enqueue("PING")
                            last_ping = time.monotonic()
                        message = broker.next_command()
                        if message:
                            sink.write(encode(message))
                            sink.flush()
            finally:
                broker.disconnect()
                executor.shutdown(wait=False, cancel_futures=True)
    finally:
        for kind, previous_handler in previous.items():
            signal.signal(kind, previous_handler)


def main():
    parser = argparse.ArgumentParser(description="AutoGrab Edge Native Messaging host")
    parser.add_argument("--root", required=True)
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("origin")
    args = parser.parse_args()
    try:
        serve(args.root, args.extension_id, args.origin)
    except Exception:
        # Never print rejected URLs, native frames, exception repr or account data.
        print("AutoGrab Edge host stopped safely.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
