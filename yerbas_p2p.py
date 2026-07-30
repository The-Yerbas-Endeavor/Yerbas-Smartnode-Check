#!/usr/bin/env python3
"""Minimal Yerbas P2P version/verack probe.

The Yerbas mainnet message start bytes are the ASCII sequence ``yerb`` and the
mainnet P2P port is 15420. This module intentionally uses only the Python
standard library so it can be imported by the smartnode checker without adding
runtime dependencies.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import struct
import time
from dataclasses import asdict, dataclass
from typing import Any

YERBAS_MAINNET_MAGIC = b"yerb"
DEFAULT_PROTOCOL = 70223
DEFAULT_USER_AGENT = b"/Yerbas-Smartnode-Check:3.0/"
HEADER_SIZE = 24
MAX_PAYLOAD = 4 * 1024 * 1024


@dataclass
class P2PProbeResult:
    success: bool = False
    version_received: bool = False
    verack_received: bool = False
    protocol: int | None = None
    services: int | None = None
    timestamp: int | None = None
    user_agent: str = ""
    start_height: int | None = None
    relay: bool | None = None
    handshake_ms: float | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256d(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot be negative")
    if value < 0xFD:
        return bytes((value,))
    if value <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", value)
    if value <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", value)
    return b"\xff" + struct.pack("<Q", value)


def decode_varint(payload: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(payload):
        raise ValueError("missing varint")
    marker = payload[offset]
    offset += 1
    if marker < 0xFD:
        return marker, offset
    sizes = {0xFD: 2, 0xFE: 4, 0xFF: 8}
    size = sizes[marker]
    if offset + size > len(payload):
        raise ValueError("truncated varint")
    fmt = {2: "<H", 4: "<I", 8: "<Q"}[size]
    return struct.unpack_from(fmt, payload, offset)[0], offset + size


def pack_network_address(ip: str, port: int, services: int = 0) -> bytes:
    parsed = ipaddress.ip_address(ip)
    if parsed.version == 4:
        packed_ip = b"\x00" * 10 + b"\xff\xff" + parsed.packed
    else:
        packed_ip = parsed.packed
    return struct.pack("<Q", services) + packed_ip + struct.pack(">H", port)


def build_message(command: str, payload: bytes = b"", magic: bytes = YERBAS_MAINNET_MAGIC) -> bytes:
    if len(magic) != 4:
        raise ValueError("network magic must contain exactly four bytes")
    encoded_command = command.encode("ascii")
    if not encoded_command or len(encoded_command) > 12:
        raise ValueError("P2P command must contain 1-12 ASCII bytes")
    return (
        magic
        + encoded_command.ljust(12, b"\x00")
        + struct.pack("<I", len(payload))
        + sha256d(payload)[:4]
        + payload
    )


def build_version_payload(
    remote_ip: str,
    remote_port: int,
    *,
    protocol: int = DEFAULT_PROTOCOL,
    start_height: int = 0,
    user_agent: bytes = DEFAULT_USER_AGENT,
) -> bytes:
    timestamp = int(time.time())
    nonce = struct.unpack("<Q", os.urandom(8))[0]
    receiver = pack_network_address(remote_ip, remote_port)
    sender = pack_network_address("0.0.0.0", 0)
    return (
        struct.pack("<iQq", protocol, 0, timestamp)
        + receiver
        + sender
        + struct.pack("<Q", nonce)
        + encode_varint(len(user_agent))
        + user_agent
        + struct.pack("<i?", start_height, False)
    )


def recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("peer closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_message(connection: socket.socket, magic: bytes) -> tuple[str, bytes]:
    header = recv_exact(connection, HEADER_SIZE)
    if header[:4] != magic:
        raise ValueError(f"unexpected network magic {header[:4].hex()}")
    command = header[4:16].rstrip(b"\x00").decode("ascii", errors="replace")
    payload_length = struct.unpack_from("<I", header, 16)[0]
    checksum = header[20:24]
    if payload_length > MAX_PAYLOAD:
        raise ValueError(f"payload too large: {payload_length} bytes")
    payload = recv_exact(connection, payload_length)
    if sha256d(payload)[:4] != checksum:
        raise ValueError(f"invalid checksum for {command}")
    return command, payload


def parse_version_payload(payload: bytes) -> dict[str, Any]:
    if len(payload) < 80:
        raise ValueError("truncated version payload")
    protocol, services, timestamp = struct.unpack_from("<iQq", payload, 0)
    offset = 80
    user_agent_length, offset = decode_varint(payload, offset)
    if offset + user_agent_length > len(payload):
        raise ValueError("truncated user agent")
    user_agent = payload[offset : offset + user_agent_length].decode("utf-8", errors="replace")
    offset += user_agent_length
    if offset + 4 > len(payload):
        raise ValueError("missing start height")
    start_height = struct.unpack_from("<i", payload, offset)[0]
    offset += 4
    relay = bool(payload[offset]) if offset < len(payload) else None
    return {
        "protocol": protocol,
        "services": services,
        "timestamp": timestamp,
        "user_agent": user_agent,
        "start_height": start_height,
        "relay": relay,
    }


def probe_peer(
    ip: str,
    port: int,
    *,
    timeout: float = 4.0,
    protocol: int = DEFAULT_PROTOCOL,
    start_height: int = 0,
    magic: bytes = YERBAS_MAINNET_MAGIC,
) -> P2PProbeResult:
    result = P2PProbeResult()
    started = time.perf_counter()
    parsed = ipaddress.ip_address(ip)
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    endpoint: tuple[Any, ...] = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)

    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(endpoint)
            version_payload = build_version_payload(
                ip,
                port,
                protocol=protocol,
                start_height=start_height,
            )
            connection.sendall(build_message("version", version_payload, magic))

            deadline = time.monotonic() + timeout
            sent_verack = False
            while time.monotonic() < deadline and not (result.version_received and result.verack_received):
                command, payload = recv_message(connection, magic)
                if command == "version":
                    parsed_version = parse_version_payload(payload)
                    result.version_received = True
                    result.protocol = parsed_version["protocol"]
                    result.services = parsed_version["services"]
                    result.timestamp = parsed_version["timestamp"]
                    result.user_agent = parsed_version["user_agent"]
                    result.start_height = parsed_version["start_height"]
                    result.relay = parsed_version["relay"]
                    if not sent_verack:
                        connection.sendall(build_message("verack", b"", magic))
                        sent_verack = True
                elif command == "verack":
                    result.verack_received = True
                elif command == "ping" and len(payload) == 8:
                    connection.sendall(build_message("pong", payload, magic))

            result.success = result.version_received and result.verack_received
            if not result.success:
                missing = []
                if not result.version_received:
                    missing.append("version")
                if not result.verack_received:
                    missing.append("verack")
                result.error = "handshake incomplete; missing " + " and ".join(missing)
    except (OSError, ValueError, ConnectionError, struct.error) as exc:
        result.error = str(exc)

    result.handshake_ms = round((time.perf_counter() - started) * 1000, 2)
    return result


__all__ = [
    "DEFAULT_PROTOCOL",
    "P2PProbeResult",
    "YERBAS_MAINNET_MAGIC",
    "build_message",
    "build_version_payload",
    "decode_varint",
    "encode_varint",
    "parse_version_payload",
    "probe_peer",
]
