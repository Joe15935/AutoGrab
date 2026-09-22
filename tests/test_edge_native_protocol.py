"""Native Messaging boundaries; entirely offline, with no Edge profile access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from autograb.edge.native_host import FrameDecoder
from autograb.edge.protocol import (MAX_FRAME_BYTES, ProtocolError, decode, encode,
                                   make_message, public_url, read_message, validate, validate_origin)


class EdgeProtocolTests(unittest.TestCase):
    def ready(self):
        return make_message("EDGE_READY", payload={"version": "0.2.1"})

    def test_frame_roundtrip_and_eof(self):
        item = self.ready()
        encoded = encode(item)
        self.assertEqual(struct.unpack("=I", encoded[:4])[0], len(encoded) - 4)
        stream = io.BytesIO(encoded)
        self.assertEqual(read_message(stream, direction="event"), item)
        self.assertIsNone(read_message(stream))

    def test_incremental_reader_split_header_body_and_concatenated_frames(self):
        decoder, first, second = FrameDecoder(), self.ready(), self.ready()
        encoded = encode(first) + encode(second)
        items = []
        for byte in encoded:
            items.extend(decoder.feed(bytes([byte])))
        self.assertEqual(items, [first, second])
        self.assertFalse(decoder.buffer)

    def test_oversize_and_zero_rejected_before_body_read(self):
        for size in (0, MAX_FRAME_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(size=size), self.assertRaises(ProtocolError):
                read_message(io.BytesIO(struct.pack("=I", size)))

    def test_truncated_header_or_body_rejected(self):
        for raw in (b"\x01", struct.pack("=I", 10) + b"{}"):
            with self.assertRaises(ProtocolError):
                read_message(io.BytesIO(raw))

    def test_partial_frame_timeout(self):
        decoder = FrameDecoder()
        decoder.feed(b"\x01")
        with patch("autograb.edge.native_host.time.monotonic", return_value=decoder.started + 6):
            with self.assertRaisesRegex(ProtocolError, "FRAME_TIMEOUT"):
                decoder.check_timeout()

    def test_slow_drip_does_not_reset_partial_frame_timeout(self):
        decoder = FrameDecoder()
        with patch("autograb.edge.native_host.time.monotonic", return_value=100):
            decoder.feed(b"\xff")
        with patch("autograb.edge.native_host.time.monotonic", return_value=104):
            decoder.feed(b"\x00")
        with patch("autograb.edge.native_host.time.monotonic", return_value=106):
            with self.assertRaisesRegex(ProtocolError, "FRAME_TIMEOUT"):
                decoder.check_timeout()

    def test_utf8_duplicate_keys_nonfinite_json_and_nesting_rejected(self):
        original = json.dumps(self.ready())
        values = [b"\xff", b"{", original.replace('"version": 1', '"version": 1, "version": 1').encode(),
                  original.replace('"payload": {', '"payload": {"amount": NaN,').encode(),
                  ("[" * 3000 + "]" * 3000).encode()]
        for value in values:
            with self.subTest(raw_length=len(value)), self.assertRaises(ProtocolError):
                decode(value)

    def test_unknown_fields_types_and_unsafe_nested_payload_fail_closed(self):
        for key, value in (("version", True), ("version", 2), ("type", "EXEC"), ("type", []),
                           ("provider", "other"), ("message_id", "x"), ("command_id", "x"),
                           ("timestamp", "2026-09-22T00:00:00"), ("payload", [])):
            changed = self.ready()
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ProtocolError):
                validate(changed)
        changed = self.ready()
        changed["cookie"] = "forbidden"
        with self.assertRaises(ProtocolError):
            validate(changed)
        for bad in ({"cookies": []}, {"version": "0.2.1", "login": {}},
                    {"version": "0.2.1", "tab_id": True}, {"version": "0.2.1", "code": "private text"}):
            changed = self.ready()
            changed["payload"] = bad
            with self.assertRaises(ProtocolError):
                validate(changed)

    def test_timestamp_stale_or_future_rejected(self):
        for seconds in (-121, 121):
            item = self.ready()
            item["timestamp"] = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
            with self.assertRaisesRegex(ProtocolError, "STALE"):
                validate(item)

    def test_exact_origin_only(self):
        extension_id = "a" * 32
        self.assertTrue(validate_origin(extension_id, f"chrome-extension://{extension_id}/"))
        for origin in (f"chrome-extension://{extension_id}", f"chrome-extension://{extension_id}/path",
                       "https://bandwagonhost.com/", "chrome-extension://" + "b" * 32 + "/"):
            with self.assertRaises(ProtocolError):
                validate_origin(extension_id, origin)

    def test_public_url_excludes_clearance_private_query_and_external_hosts(self):
        for url in ("https://bandwagonhost.com/cart.php?a=view", "https://bandwagonhost.com/clientarea.php"):
            self.assertEqual(public_url(url), url)
        for url in ("https://bandwagonhost.com/?__cf_chl_rt_tk=secret", "https://evil.invalid/",
                    "https://bandwagonhost.com.evil.invalid/", "https://user@bandwagonhost.com/",
                    "https://bandwagonhost.com/cart.php?a=complete", "https://bandwagonhost.com/cart.php?a=checkout&token=x",
                    "https://bandwagonhost.com/viewinvoice.php?id=123", "https://bandwagonhost.com/clientarea.php#secret"):
            with self.subTest(url=url), self.assertRaises(ProtocolError):
                public_url(url)

    def test_product_command_schema_allows_no_script_selector_or_live_mode(self):
        message = make_message("START_DRY_RUN", intent_id=str(uuid4()), product_id="87", payload={
            "mode": "DRY_RUN", "product": {"name": "Fixture", "url": "https://bandwagonhost.com/order/ecommerce",
                                           "period": "annually", "cents": 4999, "currency": "USD"}})
        for path, value in (("mode", "LIVE"), ("selector", "button"), ("script", "alert(1)")):
            changed = deepcopy(message)
            changed["payload"][path] = value
            with self.assertRaises(ProtocolError):
                validate(changed)
        for field, value in (("cents", True), ("period", {}), ("url", "https://bandwagonhost.com/cart.php?a=add")):
            changed = deepcopy(message)
            changed["payload"]["product"][field] = value
            with self.assertRaises(ProtocolError):
                validate(changed)

    def test_direction_and_control_identity_checked(self):
        with self.assertRaises(ProtocolError):
            validate(self.ready(), direction="command")
        with self.assertRaises(ProtocolError):
            make_message("PING", intent_id=str(uuid4()), product_id="87")
        with self.assertRaises(ProtocolError):
            make_message("CANCEL_INTENT")

    def test_python_javascript_share_positive_and_negative_contract_corpus(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is needed for the cross-language contract test")
        base = make_message("START_DRY_RUN", intent_id=str(uuid4()), product_id="87", payload={
            "mode": "DRY_RUN", "product": {"name": "Fixture", "url": "https://bandwagonhost.com/order/ecommerce/Los%20Angeles/USCA_9",
                                           "period": "annually", "cents": 4999, "currency": "USD"}})
        cases = [("valid product", base, "command"), ("valid heartbeat", self.ready(), "event")]
        for field, values in {
            "url": ["https://bandwagonhost.com/order/ecommerce", "https://bandwagonhost.com/order/ecommerce/",
                    "https://bandwagonhost.com/order/ecommerce/A/B/", "https://bandwagonhost.com/order/ecommerce/A",
                    "https://bandwagonhost.com/order/ecommerce/A/B/C", "https://bandwagonhost.com/x/../order/ecommerce/A/B",
                    "https://bandwagonhost.com/order/ecommerce/../A/B", "https://bandwagonhost.com/order/ecommerce/中国/B",
                    "https://bandwagonhost.com/order/ecommerce/A/B?secret=x", "https://bandwagonhost.com/order/ecommerce/A/B#x",
                    "https://bandwagonhost.com.evil.invalid/order/ecommerce/A/B", "HTTPS://bandwagonhost.com/order/ecommerce/A/B",
                    "https://bandwagonhost.com:443/order/ecommerce/A/B", "https://bandwagonhost.com/order/ecommerce/A/%0A"],
            "cents": [0, True, 1.5, 100000001, 999999999999, 1000000000000],
            "currency": ["USD", "EUR", []], "period": ["biennially", "monthly", {}],
            "name": ["中文产品", "", "x\x7f", "x" * 201],
        }.items():
            for index, value in enumerate(values):
                changed = deepcopy(base)
                changed["payload"]["product"][field] = value
                cases.append((f"product {field} {index}", changed, "command"))
        for field, values in {"product_id": ["9" * 40, "9" * 41, "0", "087", 87],
                              "message_id": [str(uuid4()).upper(), "00000000-0000-0000-0000-000000000000", "not-uuid"],
                              "version": [True, 2], "provider": ["other", {}]}.items():
            for index, value in enumerate(values):
                changed = deepcopy(base)
                changed[field] = value
                cases.append((f"envelope {field} {index}", changed, "command"))
        for field, values in {"version": ["99999.0.0", "1.2.3"], "tab_id": [True, -1, 2**31, 2**31-1],
                              "cart_id": ["0", "configuration_0", "a.b:c-d", "_bad"],
                              "url": ["https://bandwagonhost.com/cart.php?a=add", "https://bandwagonhost.com/viewinvoice.php",
                                      "https://bandwagonhost.com/cart.php?a=checkout", "https://bandwagonhost.com/order/ecommerce"]}.items():
            for index, value in enumerate(values):
                changed = self.ready()
                changed["payload"][field] = value
                cases.append((f"event {field} {index}", changed, "event"))
        expected = []
        for _label, value, direction in cases:
            try:
                validate(value, direction=direction)
                expected.append(True)
            except ProtocolError:
                expected.append(False)
        script = """import {readFileSync} from 'node:fs';
const {validateEnvelope} = await import(process.argv[1]);
const cases = JSON.parse(readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(([, value, direction]) => {
  try { validateEnvelope(value, direction); return true; } catch { return false; }
})));"""
        module = (Path(__file__).resolve().parents[1] / "edge-extension/protocol.js").as_uri()
        completed = subprocess.run([node, "--input-type=module", "-e", script, module],
                                   input=json.dumps(cases), text=True, capture_output=True, check=True)
        actual = json.loads(completed.stdout)
        for (label, _value, _direction), python_ok, javascript_ok in zip(cases, expected, actual):
            with self.subTest(label=label):
                self.assertEqual(python_ok, javascript_ok)


if __name__ == "__main__":
    unittest.main()
