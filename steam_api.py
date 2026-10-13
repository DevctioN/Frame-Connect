"""Small standard-library client for Steam's loopback control interface."""

import base64
import hashlib
from http.client import HTTPException
import json
import os
import socket
import struct
import time
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener


def _receive(connection, length, deadline):
    data = bytearray()
    while len(data) < length:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Steam query timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise OSError("Steam closed its connection")
        data.extend(chunk)
    return bytes(data)


def _send_frame(connection, opcode, payload):
    mask = os.urandom(4)
    header = bytes((0x80 | opcode, 0x80 | len(payload))) if len(payload) < 126 else (
        bytes((0x80 | opcode, 0xfe)) + struct.pack("!H", len(payload))
    )
    connection.sendall(header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))


def _receive_message(connection, deadline):
    message = bytearray()
    started = False
    while True:
        first, second = _receive(connection, 2, deadline)
        opcode, final, length = first & 15, bool(first & 128), second & 127
        if first & 112 or second & 128:
            raise ValueError("Invalid Steam WebSocket frame")
        if length == 126:
            length = struct.unpack("!H", _receive(connection, 2, deadline))[0]
        elif length == 127:
            length = struct.unpack("!Q", _receive(connection, 8, deadline))[0]
        if length + len(message) > 256 * 1024:
            raise ValueError("Steam response is too large")
        if opcode in (8, 9, 10) and (not final or length > 125):
            raise ValueError("Invalid Steam WebSocket control frame")
        payload = _receive(connection, length, deadline)
        if opcode == 9:
            _send_frame(connection, 10, payload)
            continue
        if opcode == 10:
            continue
        if opcode not in (0, 1) or (opcode == 0) != started:
            raise ValueError("Unexpected Steam WebSocket message")
        started = True
        message.extend(payload)
        if final:
            return json.loads(message)


def query_steam(expression: str):
    """Evaluate one expression through Steam's existing local interface.

    Return None if the interface is absent, refuses the request, or times out.
    This helper never opens a listening port or enables Steam debugging.
    """
    deadline = time.monotonic() + 2
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open("http://127.0.0.1:8080/json/list", timeout=1) as response:
            data = response.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            return None
        target = next(item for item in json.loads(data) if item.get("title") == "SharedJSContext")
        address = urlsplit(target["webSocketDebuggerUrl"])
        if (address.scheme != "ws" or address.hostname != "127.0.0.1" or address.port != 8080
                or not address.path.startswith("/devtools/page/") or address.query or address.fragment
                or address.username or address.password):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        with socket.create_connection(("127.0.0.1", 8080), timeout=remaining) as connection:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {address.path} HTTP/1.1\r\nHost: 127.0.0.1:8080\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            connection.sendall(request.encode("ascii"))
            headers = bytearray()
            while not headers.endswith(b"\r\n\r\n"):
                if len(headers) >= 8192:
                    raise ValueError("Steam handshake is too large")
                headers.extend(_receive(connection, 1, deadline))
            lines = headers.decode("ascii").split("\r\n")
            fields = {name.lower(): value.strip() for name, value in (
                line.split(":", 1) for line in lines[1:] if line
            )}
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
            if (lines[0].split()[1] != "101" or fields.get("sec-websocket-accept") != expected
                    or fields.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in [token.strip() for token in fields.get("connection", "").lower().split(",")]):
                return None
            _send_frame(connection, 1, json.dumps({
                "id": 1, "method": "Runtime.evaluate", "params": {
                    "expression": expression, "awaitPromise": True, "returnByValue": True,
                },
            }).encode("utf-8"))
            for _ in range(8):
                result = _receive_message(connection, deadline)
                if result.get("id") == 1:
                    return result["result"]["result"].get("value")
    except (OSError, HTTPException, ValueError, TypeError, KeyError, IndexError, AttributeError, StopIteration):
        pass
    return None


