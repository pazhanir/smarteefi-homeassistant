"""
Smarteefi UDP Protocol — Pure Python packet builder, parser, and async sender.

Replaces the proprietary CLI binary with direct UDP communication.
All multi-byte integers are LITTLE-ENDIAN on the wire (except txn which is big-endian).

Packet types:
  - get-status  (0x1130): 64-byte packet, queries switch/status bitmasks
  - set-status  (0x10CC): 64-byte packet, toggles channels on/off
  - set-speed   (0x12C0): 80-byte packet, sets fan speed (0-4)
  - set-intensity (0x12C0): 80-byte packet, sets light intensity (0-100)
  - set-rgb-color (0x10CC): 64-byte packet, sets RGB color for lights

Response format (66 bytes for get/set-digital):
  - 0xAAAA preamble, response_type (LE), payload_size (LE), txn (BE),
    header, serial, result, error, switchMap (LE), statusMap (LE)
"""

import asyncio
import logging
import random
import socket
import struct
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTROL_PORT = 10201       # Devices listen on this port
PUSH_PORT = 8890           # Devices push updates to this port

# Packet types (written as little-endian uint16 on the wire)
PKT_SET_DIGITAL = 0x10CC   # set-status, set-rgb-color, set-rgb-music-sync
PKT_GET_DIGITAL = 0x1130   # get-status
PKT_SET_ANALOG  = 0x12C0   # set-speed, set-intensity

# Response types = request type + 1
RESP_SET_DIGITAL = 0x10CD
RESP_GET_DIGITAL = 0x1131
RESP_SET_ANALOG  = 0x12C1

# Packet sizes
PKT_SIZE_64 = 64   # get-status, set-status
PKT_SIZE_80 = 80   # set-speed, set-intensity

# Default timeout for UDP send/receive
DEFAULT_TIMEOUT = 3.0


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def _build_header(buf: bytearray, pkt_type: int, payload_size: int, serial: str) -> None:
    """
    Fill the common header portion of an outgoing packet.

    Offset 0-1:   uint16 packet_type (LITTLE-ENDIAN)
    Offset 2-3:   uint16 payload_size (LITTLE-ENDIAN)
    Offset 4-7:   uint32 txn (random, BIG-ENDIAN)
    Offset 8-23:  16 bytes all 0x01 (protocol flags)
    Offset 24-39: 16 bytes serial (null-terminated ASCII, zero-padded)
    Offset 40-47: 8 bytes zeros
    """
    struct.pack_into("<H", buf, 0, pkt_type)
    struct.pack_into("<H", buf, 2, payload_size)
    struct.pack_into(">I", buf, 4, random.randint(0, 0xFFFFFFFF))

    # Protocol flags: 16 bytes of 0x01
    for i in range(8, 24):
        buf[i] = 0x01

    # Serial (up to 15 ASCII chars + null terminator, zero-padded to 16)
    serial_bytes = serial.encode("ascii")[:15]
    buf[24:24 + len(serial_bytes)] = serial_bytes


def build_get_status(serial: str, switch_map: int = 0xFFFFFFFF) -> bytes:
    """Build a get-status (0x1130) packet — 64 bytes."""
    buf = bytearray(PKT_SIZE_64)
    _build_header(buf, PKT_GET_DIGITAL, 0x10, serial)
    struct.pack_into("<I", buf, 48, switch_map)
    return bytes(buf)


def build_set_status(serial: str, switch_map: int, status_value: int,
                     extra: int = 0) -> bytes:
    """
    Build a set-status (0x10CC) packet — 64 bytes.

    To turn ON:  switch_map=channel_mask, status_value=channel_mask
    To turn OFF: switch_map=channel_mask, status_value=0
    """
    buf = bytearray(PKT_SIZE_64)
    _build_header(buf, PKT_SET_DIGITAL, 0x10, serial)
    struct.pack_into("<I", buf, 48, switch_map)
    struct.pack_into("<I", buf, 52, status_value)
    struct.pack_into("<I", buf, 56, extra)
    return bytes(buf)


def build_set_speed(serial: str, switch_map: int, speed: int) -> bytes:
    """
    Build a set-speed (0x12C0) packet — 80 bytes.
    payload_size = 0x20 (32 bytes).
    """
    buf = bytearray(PKT_SIZE_80)
    _build_header(buf, PKT_SET_ANALOG, 0x20, serial)
    struct.pack_into("<I", buf, 48, switch_map)
    struct.pack_into("<I", buf, 52, speed)
    return bytes(buf)


def build_set_intensity(serial: str, switch_map: int, intensity: int) -> bytes:
    """
    Build a set-intensity (0x12C0) packet — 80 bytes.
    Same packet type as set-speed.
    """
    buf = bytearray(PKT_SIZE_80)
    _build_header(buf, PKT_SET_ANALOG, 0x20, serial)
    struct.pack_into("<I", buf, 48, switch_map)
    struct.pack_into("<I", buf, 52, intensity)
    return bytes(buf)


def build_set_rgb_color(serial: str, switch_map: int, r: int, g: int, b: int) -> bytes:
    """
    Build a set-rgb-color (0x10CC) packet — 64 bytes.

    The CLI binary packs RGB as: (r << 24) | (g << 16) | (b << 8)
    This value goes in the status_value field (offset 52).
    """
    rgb_value = (r << 24) | (g << 16) | (b << 8)
    buf = bytearray(PKT_SIZE_64)
    _build_header(buf, PKT_SET_DIGITAL, 0x10, serial)
    struct.pack_into("<I", buf, 48, switch_map)
    struct.pack_into("<I", buf, 52, rgb_value)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def parse_response(data: bytes) -> Optional[dict]:
    """
    Parse a response packet from a Smarteefi device.

    Response format (66 bytes for get/set-digital):
      Offset 0-1:   0xAAAA preamble
      Offset 2-3:   uint16 response_type (LITTLE-ENDIAN)
      Offset 4-5:   uint16 payload_size (LITTLE-ENDIAN)
      Offset 6-9:   uint32 txn (BIG-ENDIAN, echoed back)
      Offset 10-25: header data
      Offset 26-41: serial (16 bytes, null-terminated)
      Offset 42-49: additional header
      Offset 50-53: uint32 result (LITTLE-ENDIAN) — 1 = success
      Offset 54-57: uint32 error (LITTLE-ENDIAN) — 0 = none
      Offset 58-61: uint32 switchMap (LITTLE-ENDIAN)
      Offset 62-65: uint32 statusMap (LITTLE-ENDIAN)

    Returns dict with parsed fields, or None if packet is too short.
    """
    if len(data) < 58:
        return None

    resp_type = struct.unpack_from("<H", data, 2)[0]
    payload_size = struct.unpack_from("<H", data, 4)[0]
    txn = struct.unpack_from(">I", data, 6)[0]

    serial = data[26:42].split(b"\x00")[0].decode("ascii", errors="replace").strip()

    result = struct.unpack_from("<I", data, 50)[0]
    error = struct.unpack_from("<I", data, 54)[0]

    parsed = {
        "response_type": resp_type,
        "serial": serial,
        "result": result,
        "error": error,
    }

    # Get/Set digital responses have switchMap + statusMap
    if resp_type in (RESP_GET_DIGITAL, RESP_SET_DIGITAL) and len(data) >= 66:
        parsed["switchmap"] = struct.unpack_from("<I", data, 58)[0]
        parsed["statusmap"] = struct.unpack_from("<I", data, 62)[0]

    # Set analog response
    elif resp_type == RESP_SET_ANALOG and len(data) >= 62:
        parsed["switchmap"] = struct.unpack_from("<I", data, 58)[0]
        if len(data) >= 66:
            parsed["value"] = struct.unpack_from("<I", data, 62)[0]

    return parsed


def parse_push_update(data: bytes) -> Optional[dict]:
    """
    Parse a push update received on port 8890.

    Format (minimum 26 bytes):
      Offset 0-15:  serial (16 bytes, null-terminated ASCII)
      Offset 16:    ':' separator (0x3A)
      Offset 17-20: switchmap (4 bytes little-endian uint32)
      Offset 21:    ':' separator (0x3A)
      Offset 22-25: status (4 bytes little-endian uint32)
    """
    if len(data) < 26:
        return None

    if data[16] != 0x3A or data[21] != 0x3A:
        return None

    serial = data[0:16].split(b"\x00")[0].decode("ascii", errors="replace").strip()
    if not serial:
        return None

    switchmap = struct.unpack_from("<I", data, 17)[0]
    status = struct.unpack_from("<I", data, 22)[0]

    return {
        "serial": serial,
        "switchmap": switchmap,
        "status": status,
    }


# ---------------------------------------------------------------------------
# Async UDP send/receive
# ---------------------------------------------------------------------------

async def async_send_and_receive(
    packet: bytes,
    broadcast_addr: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """
    Send a UDP packet to broadcast_addr:CONTROL_PORT and wait for a response.

    Uses asyncio for non-blocking I/O. Returns parsed response dict or None.
    """
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    transport = None

    class _ResponseProtocol(asyncio.DatagramProtocol):
        def connection_made(self, t):
            nonlocal transport
            transport = t

        def datagram_received(self, data, addr):
            if not future.done():
                future.set_result((data, addr))

        def error_received(self, exc):
            _LOGGER.error("UDP error: %s", exc)
            if not future.done():
                future.set_exception(exc)

    try:
        transport, _ = await loop.create_datagram_endpoint(
            _ResponseProtocol,
            local_addr=("0.0.0.0", 0),
            family=socket.AF_INET,
        )

        # Enable broadcast
        sock = transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        transport.sendto(packet, (broadcast_addr, CONTROL_PORT))

        data, addr = await asyncio.wait_for(future, timeout=timeout)
        _LOGGER.debug("UDP response from %s (%d bytes)", addr, len(data))

        return parse_response(data)

    except asyncio.TimeoutError:
        _LOGGER.debug("UDP send_and_receive timed out after %.1fs", timeout)
        return None
    except Exception as e:
        _LOGGER.error("UDP send_and_receive error: %s", e)
        return None
    finally:
        if transport:
            transport.close()


# ---------------------------------------------------------------------------
# High-level async commands (used by entity platform files)
# ---------------------------------------------------------------------------

async def async_get_status(
    serial: str,
    broadcast_addr: str,
    switch_map: int = 0xFFFFFFFF,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """
    Send get-status and return parsed response.
    Returns dict with 'switchmap' and 'statusmap' keys on success, or None.
    """
    packet = build_get_status(serial, switch_map)
    return await async_send_and_receive(packet, broadcast_addr, timeout)


async def async_set_status(
    serial: str,
    broadcast_addr: str,
    switch_map: int,
    turn_on: bool,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """
    Send set-status to turn a channel ON or OFF.
    Returns parsed response dict on success, or None.
    """
    status_value = switch_map if turn_on else 0
    packet = build_set_status(serial, switch_map, status_value)
    return await async_send_and_receive(packet, broadcast_addr, timeout)


async def async_set_speed(
    serial: str,
    broadcast_addr: str,
    switch_map: int,
    speed: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """
    Send set-speed for fan control.
    Returns parsed response dict on success, or None.
    """
    packet = build_set_speed(serial, switch_map, speed)
    return await async_send_and_receive(packet, broadcast_addr, timeout)


async def async_set_intensity(
    serial: str,
    broadcast_addr: str,
    switch_map: int,
    intensity: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """
    Send set-intensity for light brightness.
    Returns parsed response dict on success, or None.
    """
    packet = build_set_intensity(serial, switch_map, intensity)
    return await async_send_and_receive(packet, broadcast_addr, timeout)


async def async_set_rgb_color(
    serial: str,
    broadcast_addr: str,
    switch_map: int,
    r: int, g: int, b: int,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """
    Send set-rgb-color for light color control.
    Returns parsed response dict on success, or None.
    """
    packet = build_set_rgb_color(serial, switch_map, r, g, b)
    return await async_send_and_receive(packet, broadcast_addr, timeout)


def compute_broadcast_addr(ip_address: str, netmask: str) -> str:
    """Compute the broadcast address from IP and netmask."""
    ip_parts = [int(x) for x in ip_address.split(".")]
    mask_parts = [int(x) for x in netmask.split(".")]
    broadcast_parts = [(ip | (~mask & 0xFF)) for ip, mask in zip(ip_parts, mask_parts)]
    return ".".join(str(x) for x in broadcast_parts)
