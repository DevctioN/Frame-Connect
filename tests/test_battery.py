import base64
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import socket
import struct
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from headset import HeadsetError, parse_status
from probe import collect_status, read_steam_battery
from test_headset import SAMPLE


TARGETS = [{"title": "SharedJSContext", "webSocketDebuggerUrl":
            "ws://127.0.0.1:8080/devtools/page/battery-test"}]


def receive(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise OSError("Test peer closed")
        data.extend(chunk)
    return bytes(data)


def frame(opcode, payload, final=True):
    first = opcode | (128 if final else 0)
    header = bytes((first, len(payload))) if len(payload) < 126 else bytes((first, 126)) + struct.pack("!H", len(payload))
    return header + payload


@contextmanager
def steam_server(reply, *, fragmented=False, bad_handshake=False, oversized=False):
    client, server = socket.socketpair()
    errors, requests = [], []
    server.settimeout(2)

    def serve():
        try:
            with server:
                header = bytearray()
                while not header.endswith(b"\r\n\r\n"):
                    header.extend(receive(server, 1))
                fields = dict(line.split(": ", 1) for line in header.decode().split("\r\n")[1:] if line)
                accept = base64.b64encode(hashlib.sha1(
                    (fields["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()).decode()
                if bad_handshake:
                    accept = "invalid"
                server.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                if bad_handshake:
                    return
                first, second = receive(server, 2)
                assert first == 0x81 and second & 128, "Client must send masked text"
                size = second & 127
                if size == 126:
                    size = struct.unpack("!H", receive(server, 2))[0]
                mask = receive(server, 4)
                payload = receive(server, size)
                requests.append(json.loads(bytes(value ^ mask[index % 4] for index, value in enumerate(payload))))
                if oversized:
                    server.sendall(bytes((0x81, 127)) + struct.pack("!Q", 256 * 1024 + 1))
                    return
                payload = json.dumps(reply).encode()
                if fragmented:
                    middle = len(payload) // 2
                    server.sendall(frame(1, payload[:middle], False) + frame(9, b"ping"))
                    first, second = receive(server, 2)
                    assert first == 0x8a and second == 0x84, "Client must answer ping"
                    mask, pong = receive(server, 4), receive(server, 4)
                    assert bytes(value ^ mask[index % 4] for index, value in enumerate(pong)) == b"ping"
                    server.sendall(frame(0, payload[middle:]))
                else:
                    server.sendall(frame(1, payload))
        except Exception as error:
            errors.append(error)

    thread = Thread(target=serve, daemon=True)
    thread.start()
    opener = Mock()
    opener.open.return_value = BytesIO(json.dumps(TARGETS).encode())
    try:
        with patch("steam_api.build_opener", return_value=opener), patch("steam_api.socket.create_connection", return_value=client):
            yield requests
    finally:
        client.close()
        thread.join(3)
        if thread.is_alive():
            server.close()
            raise AssertionError("Battery test server did not finish")
        if errors:
            raise errors[0]


class SteamBatteryTests(unittest.TestCase):
    def test_reports_steam_percentage_including_zero_and_rounds_without_offset(self):
        for level, percent in ((0, 0), (.934, 93), (.995, 100), (1, 100)):
            with self.subTest(level=level), steam_server({"id": 1, "result": {"result": {"value": level}}}) as requests:
                self.assertEqual(read_steam_battery(), percent)
                self.assertEqual(requests[0]["method"], "Runtime.evaluate")
                self.assertTrue(requests[0]["params"]["awaitPromise"])

    def test_fragmented_response_and_ping_are_supported(self):
        with steam_server({"id": 1, "result": {"result": {"value": .99}}}, fragmented=True):
            self.assertEqual(read_steam_battery(), 99)

    def test_invalid_or_unavailable_steam_values_fall_back(self):
        for value in (None, True, "99", -1, 1.01, float("nan"), float("inf"), {}):
            with self.subTest(value=value), steam_server({"id": 1, "result": {"result": {"value": value}}}):
                self.assertIsNone(read_steam_battery())
        with steam_server({"id": 1, "error": {"message": "Steam is restarting"}}):
            self.assertIsNone(read_steam_battery())

    def test_bad_handshake_and_oversized_message_fall_back(self):
        for options in ({"bad_handshake": True}, {"oversized": True}):
            with self.subTest(options=options), steam_server({}, **options):
                self.assertIsNone(read_steam_battery())

    def test_missing_interface_and_non_loopback_targets_are_not_used(self):
        with patch("steam_api.build_opener", side_effect=OSError("Interface unavailable")):
            self.assertIsNone(read_steam_battery())
        for targets in ([], [{"title": "SharedJSContext", "webSocketDebuggerUrl":
                             "ws://192.0.2.1:8080/devtools/page/test"}]):
            opener = Mock()
            opener.open.return_value = BytesIO(json.dumps(targets).encode())
            with patch("steam_api.build_opener", return_value=opener), patch("steam_api.socket.create_connection") as connect:
                self.assertIsNone(read_steam_battery())
                connect.assert_not_called()

    def test_collector_prefers_steam_and_preserves_system_fallback_and_state(self):
        for percent in (99, 0, None):
            with self.subTest(percent=percent), patch("probe.read_batteries", return_value=[
                {"name": "max1720x", "percent": 93, "state": "Discharging"}
            ]), patch("probe.read_steam_battery", return_value=percent):
                status = parse_status(json.dumps(collect_status()))
                self.assertEqual(status.batteries[0].percent, 93 if percent is None else percent)
                self.assertEqual(status.batteries[0].source, "system" if percent is None else "steam")
                self.assertEqual(status.batteries[0].state, "Discharging")

    def test_multiple_system_batteries_are_not_replaced_with_one_steam_value(self):
        with patch("probe.read_batteries", return_value=[SAMPLE["batteries"][0].copy()] * 2), patch("probe.read_steam_battery") as query:
            self.assertEqual(len(collect_status()["batteries"]), 2)
            query.assert_not_called()

    def test_battery_source_defaults_to_system_and_rejects_invalid_sources(self):
        self.assertEqual(parse_status(json.dumps(SAMPLE)).batteries[0].source, "system")
        for value in (None, "invented", True, []):
            data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0], source=value)])
            with self.subTest(value=value), self.assertRaises(HeadsetError):
                parse_status(json.dumps(data))
