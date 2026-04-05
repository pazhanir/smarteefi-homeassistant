#!/usr/bin/env python3
"""
Smarteefi UDP Device Scanner & Controller

Standalone tool to discover and control Smarteefi devices on the local network.
No Home Assistant dependency required — just Python 3.6+.

Discovery modes:
  1. Passive (port 8890) — Listen for voluntary device push updates.
  2. Active (port 10201) — Send crafted get-status broadcast packets
     and listen for responses from devices.

Control modes:
  3. Targeted get-status — Query a specific device by serial.
  4. Set-status — Toggle a specific channel ON or OFF.
  5. Set-speed / Set-intensity — Set fan speed or light intensity.

IMPORTANT: Protocol is LITTLE-ENDIAN (discovered via tcpdump analysis).
The CLI binary's helper functions appear big-endian in disassembly, but
the actual on-wire packets are little-endian.

Usage:
  python3 smarteefi_scanner.py                              # Both modes, 30s listen
  python3 smarteefi_scanner.py --passive                    # Passive only
  python3 smarteefi_scanner.py --active                     # Active only
  python3 smarteefi_scanner.py --duration 60                # Listen for 60 seconds
  python3 smarteefi_scanner.py --target se5110000385        # Query a specific device
  python3 smarteefi_scanner.py --set se5110000385 8 on      # Turn channel 8 ON
  python3 smarteefi_scanner.py --set se5110000385 8 off     # Turn channel 8 OFF
  python3 smarteefi_scanner.py --set-speed se5110000385 8 3 # Set fan speed to 3
  python3 smarteefi_scanner.py --set-intensity se5110000385 8 50  # Set intensity to 50%
  python3 smarteefi_scanner.py --listen-push 60             # Listen on 8890 while toggling
  python3 smarteefi_scanner.py --broadcast 192.168.1.255    # Explicit broadcast addr
  python3 smarteefi_scanner.py --dump-packet se5110000385   # Print packet hex only

Requires root/sudo for binding to privileged ports (8890).
"""

import argparse
import json
import os
import random
import select
import socket
import struct
import sys
import time
import zlib
from collections import OrderedDict
from datetime import datetime


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUSH_PORT = 8890       # Devices send push updates here
CONTROL_PORT = 10201   # Devices listen for commands here / respond from here

# Packet types (from CLI reverse engineering)
# On the wire these are written as LITTLE-ENDIAN uint16
PKT_SET_DIGITAL = 0x10CC   # set-status, set-rgb-color, set-rgb-music-sync
PKT_GET_DIGITAL = 0x1130   # get-status
PKT_SET_ANALOG  = 0x12C0   # set-speed, set-intensity
PKT_SET_CONFIG  = 0x1004   # set-config
PKT_GET_CONFIG  = 0x1068   # get-config

# Response types = request type + 1
RESP_SET_DIGITAL = 0x10CD
RESP_GET_DIGITAL = 0x1131
RESP_SET_ANALOG  = 0x12C1
RESP_SET_CONFIG  = 0x1005
RESP_GET_CONFIG  = 0x1069

# Packet sizes
PKT_SIZE_64 = 64   # get-status, set-status, get-config, set-config
PKT_SIZE_80 = 80   # set-speed, set-intensity (payload_size=0x20)


# ---------------------------------------------------------------------------
# Packet builders — ALL LITTLE-ENDIAN (except txn which stays big-endian)
# ---------------------------------------------------------------------------

def _build_header(buf: bytearray, pkt_type: int, payload_size: int, serial) -> None:
    """
    Fill the common header portion of an outgoing packet.

    Offset 0-1:   uint16 packet_type (LITTLE-ENDIAN)
    Offset 2-3:   uint16 payload_size (LITTLE-ENDIAN)
    Offset 4-7:   uint32 txn (random, BIG-ENDIAN — kept as original)
    Offset 8-23:  16 bytes all 0x01 (protocol flags)
    Offset 24-39: 16 bytes serial (null-terminated ASCII, zero-padded)
    Offset 40-47: 8 bytes zeros
    """
    struct.pack_into("<H", buf, 0, pkt_type)        # LITTLE-ENDIAN
    struct.pack_into("<H", buf, 2, payload_size)     # LITTLE-ENDIAN
    struct.pack_into(">I", buf, 4, random.randint(0, 0xFFFFFFFF))  # txn stays BE

    # Protocol flags: 16 bytes of 0x01
    for i in range(8, 24):
        buf[i] = 0x01

    # Serial (up to 16 bytes, null-terminated, zero-padded)
    if isinstance(serial, bytes):
        serial_bytes = serial[:16]
    else:
        serial_bytes = serial.encode("ascii")[:15]
    buf[24:24 + len(serial_bytes)] = serial_bytes


def build_get_status_packet(serial, switch_map: int = 0xFFFFFFFF) -> bytes:
    """
    Build a get-status (0x1130) UDP packet — 64 bytes.

    Payload at offset 48: uint32 switchMap (LITTLE-ENDIAN)
    """
    buf = bytearray(PKT_SIZE_64)
    _build_header(buf, PKT_GET_DIGITAL, 0x10, serial)
    struct.pack_into("<I", buf, 48, switch_map)     # LITTLE-ENDIAN
    return bytes(buf)


def build_set_status_packet(serial, switch_map: int, status_value: int, extra: int = 0) -> bytes:
    """
    Build a set-status (0x10CC) UDP packet — 64 bytes.

    Payload at offset 48:
      uint32 switchMap    (LITTLE-ENDIAN) — which channel(s) to set
      uint32 statusValue  (LITTLE-ENDIAN) — ON=switch_map, OFF=0
      uint32 extra_arg    (LITTLE-ENDIAN) — usually 0

    To turn ON:  switch_map=channel_mask, status_value=channel_mask
    To turn OFF: switch_map=channel_mask, status_value=0
    """
    buf = bytearray(PKT_SIZE_64)
    _build_header(buf, PKT_SET_DIGITAL, 0x10, serial)
    struct.pack_into("<I", buf, 48, switch_map)     # LITTLE-ENDIAN
    struct.pack_into("<I", buf, 52, status_value)   # LITTLE-ENDIAN
    struct.pack_into("<I", buf, 56, extra)          # LITTLE-ENDIAN
    return bytes(buf)


def build_set_speed_packet(serial, switch_map: int, speed: int) -> bytes:
    """
    Build a set-speed (0x12C0) UDP packet — 80 bytes.
    payload_size = 0x20 (32 bytes of payload after the header).

    Payload at offset 48:
      uint32 switchMap  (LITTLE-ENDIAN)
      uint32 speed      (LITTLE-ENDIAN) — 0-5 typically
      ... remaining zeros

    NOTE: This has NOT been tested on real hardware yet.
    """
    buf = bytearray(PKT_SIZE_80)
    _build_header(buf, PKT_SET_ANALOG, 0x20, serial)
    struct.pack_into("<I", buf, 48, switch_map)     # LITTLE-ENDIAN
    struct.pack_into("<I", buf, 52, speed)          # LITTLE-ENDIAN
    return bytes(buf)


def build_set_intensity_packet(serial, switch_map: int, intensity: int) -> bytes:
    """
    Build a set-intensity (0x12C0) UDP packet — 80 bytes.
    Same packet type as set-speed (0x12C0).

    Payload at offset 48:
      uint32 switchMap  (LITTLE-ENDIAN)
      uint32 intensity  (LITTLE-ENDIAN) — 0-100 typically
      ... remaining zeros

    NOTE: This has NOT been tested on real hardware yet.
    """
    buf = bytearray(PKT_SIZE_80)
    _build_header(buf, PKT_SET_ANALOG, 0x20, serial)
    struct.pack_into("<I", buf, 48, switch_map)     # LITTLE-ENDIAN
    struct.pack_into("<I", buf, 52, intensity)      # LITTLE-ENDIAN
    return bytes(buf)


def build_get_config_packet(serial, param_id: int = 1) -> bytes:
    """
    Build a get-config (0x1068) packet — 64 bytes.
    param_id=1 requests WiFi config.
    """
    buf = bytearray(PKT_SIZE_64)
    _build_header(buf, PKT_GET_CONFIG, 0x10, serial)
    struct.pack_into("<I", buf, 48, param_id)       # LITTLE-ENDIAN
    return bytes(buf)


# ---------------------------------------------------------------------------
# Packet parsers — ALL LITTLE-ENDIAN
# ---------------------------------------------------------------------------

def parse_push_update(data: bytes) -> dict | None:
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

    # Check separators
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


def parse_response_packet(data: bytes) -> dict | None:
    """
    Parse a response packet received on port 10201.

    Response format (66 bytes for get-status):
      Offset 0-1:   0xAAAA preamble
      Offset 2-3:   uint16 response_type (LITTLE-ENDIAN)
      Offset 4-5:   uint16 payload_size (LITTLE-ENDIAN)
      Offset 6-9:   uint32 txn (echoed back, BIG-ENDIAN)
      Offset 10-25: header data
      Offset 26-41: serial (16 bytes, null-terminated)
      Offset 42-49: additional header
      Offset 50-53: uint32 result (LITTLE-ENDIAN) — 1 = success
      Offset 54-57: uint32 error (LITTLE-ENDIAN) — 0 = none
      Offset 58-61: uint32 switchMap (LITTLE-ENDIAN)
      Offset 62-65: uint32 statusMap (LITTLE-ENDIAN)
    """
    if len(data) < 58:
        return None

    resp_type = struct.unpack_from("<H", data, 2)[0]     # LITTLE-ENDIAN
    payload_size = struct.unpack_from("<H", data, 4)[0]   # LITTLE-ENDIAN
    txn = struct.unpack_from(">I", data, 6)[0]            # txn echoed as-is

    # Serial is at offset 26 in the response
    serial = data[26:42].split(b"\x00")[0].decode("ascii", errors="replace").strip()

    result = struct.unpack_from("<I", data, 50)[0]        # LITTLE-ENDIAN
    error = struct.unpack_from("<I", data, 54)[0]         # LITTLE-ENDIAN

    parsed = {
        "response_type": resp_type,
        "response_type_hex": f"0x{resp_type:04X}",
        "payload_size": payload_size,
        "txn": txn,
        "serial": serial,
        "result": result,
        "error": error,
    }

    # GetDigital response (0x1131) — has switchMap + statusMap
    if resp_type == RESP_GET_DIGITAL and len(data) >= 66:
        parsed["switchmap"] = struct.unpack_from("<I", data, 58)[0]   # LITTLE-ENDIAN
        parsed["statusmap"] = struct.unpack_from("<I", data, 62)[0]   # LITTLE-ENDIAN

    # SetDigital response (0x10CD) — has switchMap + statusMap
    elif resp_type == RESP_SET_DIGITAL and len(data) >= 66:
        parsed["switchmap"] = struct.unpack_from("<I", data, 58)[0]
        parsed["statusmap"] = struct.unpack_from("<I", data, 62)[0]

    # SetAnalog response (0x12C1) — basic parse
    elif resp_type == RESP_SET_ANALOG and len(data) >= 62:
        parsed["switchmap"] = struct.unpack_from("<I", data, 58)[0]
        if len(data) >= 66:
            parsed["value"] = struct.unpack_from("<I", data, 62)[0]

    # GetConfig response (0x1069) — raw data
    elif resp_type == RESP_GET_CONFIG and len(data) > 58:
        parsed["config_data"] = data[58:].hex()

    return parsed


# ---------------------------------------------------------------------------
# Discovery modes
# ---------------------------------------------------------------------------

def passive_discovery(duration: float, bind_addr: str = "") -> dict:
    """
    Listen on port 8890 for push updates from devices.
    Returns dict of serial -> device info.
    """
    devices = OrderedDict()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # SO_REUSEPORT if available (macOS, Linux 3.9+)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    try:
        sock.bind((bind_addr, PUSH_PORT))
    except PermissionError:
        print(f"  [!] Permission denied binding to port {PUSH_PORT}.")
        print(f"      Try running with sudo: sudo python3 {sys.argv[0]}")
        sock.close()
        return devices
    except OSError as e:
        print(f"  [!] Cannot bind to port {PUSH_PORT}: {e}")
        sock.close()
        return devices

    sock.setblocking(False)
    print(f"  Listening on 0.0.0.0:{PUSH_PORT} for {duration:.0f}s...")

    start = time.monotonic()
    packet_count = 0

    while time.monotonic() - start < duration:
        remaining = duration - (time.monotonic() - start)
        if remaining <= 0:
            break

        ready, _, _ = select.select([sock], [], [], min(remaining, 1.0))
        if not ready:
            elapsed = time.monotonic() - start
            # Print progress every 5 seconds
            if int(elapsed) % 5 == 0 and int(elapsed) > 0:
                sys.stdout.write(
                    f"\r  [{elapsed:.0f}s/{duration:.0f}s] "
                    f"Packets: {packet_count} | Devices: {len(devices)}"
                )
                sys.stdout.flush()
            continue

        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            continue

        packet_count += 1
        parsed = parse_push_update(data)

        if parsed:
            serial = parsed["serial"]
            now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            if serial not in devices:
                devices[serial] = {
                    "serial": serial,
                    "switchmap": parsed["switchmap"],
                    "status": parsed["status"],
                    "source_ip": addr[0],
                    "source_port": addr[1],
                    "first_seen": now,
                    "last_seen": now,
                    "update_count": 1,
                }
                print(f"\n  [+] NEW DEVICE: {serial} from {addr[0]}:{addr[1]}"
                      f" | smap=0x{parsed['switchmap']:08X}"
                      f" status=0x{parsed['status']:08X}")
            else:
                old_status = devices[serial]["status"]
                devices[serial]["switchmap"] = parsed["switchmap"]
                devices[serial]["status"] = parsed["status"]
                devices[serial]["last_seen"] = now
                devices[serial]["update_count"] += 1
                devices[serial]["source_ip"] = addr[0]

                if old_status != parsed["status"]:
                    print(f"\n  [~] STATUS CHANGE: {serial}"
                          f" | 0x{old_status:08X} -> 0x{parsed['status']:08X}"
                          f" @ {now}")
                else:
                    sys.stdout.write(
                        f"\r  [{serial}] update #{devices[serial]['update_count']}"
                        f" @ {now} (no change)     "
                    )
                    sys.stdout.flush()
        else:
            print(f"\n  [?] Unparseable packet from {addr[0]}:{addr[1]}"
                  f" ({len(data)} bytes): {data[:32].hex()}")

    print(f"\n  Done. Received {packet_count} packets, found {len(devices)} device(s).")
    sock.close()
    return devices


def active_discovery(broadcast_addr: str, duration: float, bind_addr: str = "") -> dict:
    """
    Send get-status broadcast packets on port 10201 with various serial
    patterns and listen for responses.
    Returns dict of serial -> device info.
    """
    devices = OrderedDict()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    try:
        sock.bind((bind_addr, 0))  # Bind to any available port
    except OSError as e:
        print(f"  [!] Cannot bind socket: {e}")
        sock.close()
        return devices

    sock.setblocking(False)

    # Serial patterns to try for broadcast discovery
    # The device checks if the serial in the packet matches its own.
    # We try various patterns to see if any cause all devices to respond.
    probe_serials = [
        ("empty", b""),                       # Empty serial (all zeros)
        ("all-null", b"\x00" * 16),           # Explicit all null bytes
        ("all-0xFF", b"\xff" * 16),           # All 0xFF
        ("wildcard-*", b"*"),                 # Wildcard asterisk
        ("ASCII-F", b"FFFFFFFFFFFFFFFF"),     # ASCII F's
        ("ASCII-0", b"000000000000"),         # ASCII zeros
        ("SMARTEEFI", b"SMARTEEFI"),          # Brand name guess
        ("BROADCAST", b"BROADCAST"),          # Broadcast keyword
    ]

    # Packet types to try
    probe_packets = []
    for label, serial_bytes in probe_serials:
        # get-status with switchmap=0xFFFFFFFF (all channels)
        probe_packets.append(("get-status", label, build_get_status_packet(serial_bytes)))
        # get-config with paramId=1
        probe_packets.append(("get-config", label, build_get_config_packet(serial_bytes)))

    print(f"  Broadcasting {len(probe_packets)} probe packets to {broadcast_addr}:{CONTROL_PORT}")
    print(f"  Then listening for {duration:.0f}s...")

    # Send all probes
    for probe_name, label, packet in probe_packets:
        try:
            sock.sendto(packet, (broadcast_addr, CONTROL_PORT))
            print(f"    Sent {probe_name} | serial={label} | {len(packet)} bytes")
        except OSError as e:
            print(f"    [!] Failed to send {probe_name} serial={label}: {e}")
        time.sleep(0.05)  # Small delay between probes

    # Listen for responses
    print(f"\n  Listening for responses on port {sock.getsockname()[1]}...")
    start = time.monotonic()
    response_count = 0

    while time.monotonic() - start < duration:
        remaining = duration - (time.monotonic() - start)
        if remaining <= 0:
            break

        ready, _, _ = select.select([sock], [], [], min(remaining, 1.0))
        if not ready:
            continue

        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            continue

        response_count += 1
        parsed = parse_response_packet(data)

        if parsed and parsed.get("serial"):
            serial = parsed["serial"]
            now = datetime.now().strftime("%H:%M:%S")

            if serial not in devices:
                devices[serial] = {
                    "serial": serial,
                    "source_ip": addr[0],
                    "source_port": addr[1],
                    "response_type": parsed["response_type_hex"],
                    "result": parsed["result"],
                    "error": parsed["error"],
                    "first_seen": now,
                }

                detail = ""
                if "switchmap" in parsed:
                    devices[serial]["switchmap"] = parsed["switchmap"]
                    devices[serial]["statusmap"] = parsed["statusmap"]
                    detail = (f" | smap=0x{parsed['switchmap']:08X}"
                              f" status=0x{parsed['statusmap']:08X}")

                print(f"  [+] RESPONSE from {serial} @ {addr[0]}:{addr[1]}"
                      f" | type={parsed['response_type_hex']}"
                      f" | result={parsed['result']}{detail}")
            else:
                devices[serial]["last_response"] = now
        else:
            print(f"  [?] Raw response from {addr[0]}:{addr[1]}"
                  f" ({len(data)} bytes): {data.hex()}")

    # Send a second round of probes if we got no responses
    if response_count == 0:
        print(f"\n  No responses yet. Sending repeat probes...")
        for probe_name, label, packet in probe_packets[:4]:
            try:
                sock.sendto(packet, (broadcast_addr, CONTROL_PORT))
            except OSError:
                pass
            time.sleep(0.05)

        # Listen again briefly
        extra_start = time.monotonic()
        while time.monotonic() - extra_start < 5:
            ready, _, _ = select.select([sock], [], [], 1.0)
            if not ready:
                continue
            try:
                data, addr = sock.recvfrom(1024)
            except OSError:
                continue

            response_count += 1
            parsed = parse_response_packet(data)
            if parsed and parsed.get("serial"):
                serial = parsed["serial"]
                devices[serial] = {
                    "serial": serial,
                    "source_ip": addr[0],
                    "response_type": parsed["response_type_hex"],
                    "result": parsed["result"],
                    "error": parsed["error"],
                }
                print(f"  [+] RESPONSE from {serial} @ {addr[0]}:{addr[1]}")
            else:
                print(f"  [?] Raw: {addr[0]}:{addr[1]} ({len(data)}b): {data.hex()}")

    print(f"\n  Done. Received {response_count} responses, found {len(devices)} device(s).")
    sock.close()
    return devices


# ---------------------------------------------------------------------------
# Targeted operations
# ---------------------------------------------------------------------------

def _send_and_receive(broadcast_addr: str, packet: bytes, serial: str,
                      label: str, bind_addr: str = "",
                      timeout: float = 3.0) -> dict | None:
    """
    Send a UDP packet and wait for a response. Common helper for all
    targeted operations.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    try:
        sock.bind((bind_addr, 0))
    except OSError as e:
        print(f"  [!] Cannot bind: {e}")
        sock.close()
        return None

    sock.settimeout(timeout)

    print(f"  Sending {label} for '{serial}' to {broadcast_addr}:{CONTROL_PORT}")
    print(f"  Packet ({len(packet)} bytes): {packet.hex()}")

    try:
        sock.sendto(packet, (broadcast_addr, CONTROL_PORT))
    except OSError as e:
        print(f"  [!] Send failed: {e}")
        sock.close()
        return None

    print(f"  Waiting for response ({timeout:.0f}s timeout)...")

    try:
        data, addr = sock.recvfrom(1024)
        print(f"  [+] Got response from {addr[0]}:{addr[1]} ({len(data)} bytes)")
        print(f"      Raw: {data.hex()}")

        parsed = parse_response_packet(data)
        if parsed:
            print(f"      Parsed: {json.dumps(parsed, indent=2)}")
        sock.close()
        return parsed

    except socket.timeout:
        print(f"  [-] No response (timed out)")
        sock.close()
        return None


def targeted_get_status(broadcast_addr: str, serial: str,
                        bind_addr: str = "") -> dict | None:
    """Send a get-status packet targeting a specific known serial."""
    packet = build_get_status_packet(serial)
    return _send_and_receive(broadcast_addr, packet, serial, "get-status", bind_addr)


def targeted_set_status(broadcast_addr: str, serial: str, channel_map: int,
                        turn_on: bool, bind_addr: str = "") -> dict | None:
    """
    Send a set-status packet to toggle a specific channel ON or OFF.

    turn_on=True:  status_value = channel_map (set the bits)
    turn_on=False: status_value = 0 (clear the bits)
    """
    status_value = channel_map if turn_on else 0
    action = "ON" if turn_on else "OFF"
    packet = build_set_status_packet(serial, channel_map, status_value)
    print(f"  Action: set channel_map={channel_map} (0x{channel_map:X}) to {action}")
    return _send_and_receive(broadcast_addr, packet, serial,
                             f"set-status ({action})", bind_addr)


def targeted_set_speed(broadcast_addr: str, serial: str, channel_map: int,
                       speed: int, bind_addr: str = "") -> dict | None:
    """Send a set-speed packet for fan control."""
    packet = build_set_speed_packet(serial, channel_map, speed)
    print(f"  Action: set channel_map={channel_map} speed={speed}")
    return _send_and_receive(broadcast_addr, packet, serial,
                             f"set-speed (speed={speed})", bind_addr)


def targeted_set_intensity(broadcast_addr: str, serial: str, channel_map: int,
                           intensity: int, bind_addr: str = "") -> dict | None:
    """Send a set-intensity packet for light brightness."""
    packet = build_set_intensity_packet(serial, channel_map, intensity)
    print(f"  Action: set channel_map={channel_map} intensity={intensity}")
    return _send_and_receive(broadcast_addr, packet, serial,
                             f"set-intensity (intensity={intensity})", bind_addr)


# ---------------------------------------------------------------------------
# Push update listener (for testing push updates during state changes)
# ---------------------------------------------------------------------------

def listen_push_updates(duration: float, bind_addr: str = "") -> None:
    """
    Listen on port 8890 for push updates. Optimized for testing whether
    devices send push updates on state change.

    Run this in one terminal while toggling a switch from HA or the app.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    try:
        sock.bind((bind_addr, PUSH_PORT))
    except PermissionError:
        print(f"  [!] Permission denied binding to port {PUSH_PORT}.")
        print(f"      Try: sudo python3 {sys.argv[0]} --listen-push {duration:.0f}")
        sock.close()
        return
    except OSError as e:
        print(f"  [!] Cannot bind to port {PUSH_PORT}: {e}")
        sock.close()
        return

    sock.setblocking(False)
    print(f"  Listening on 0.0.0.0:{PUSH_PORT} for push updates ({duration:.0f}s)")
    print(f"  Toggle a switch NOW to see if a push update arrives...")
    print(f"  (Ctrl+C to stop early)\n")

    start = time.monotonic()
    count = 0

    try:
        while time.monotonic() - start < duration:
            remaining = duration - (time.monotonic() - start)
            if remaining <= 0:
                break

            ready, _, _ = select.select([sock], [], [], min(remaining, 0.5))
            if not ready:
                elapsed = time.monotonic() - start
                sys.stdout.write(f"\r  Waiting... {elapsed:.1f}s / {duration:.0f}s | packets: {count}")
                sys.stdout.flush()
                continue

            try:
                data, addr = sock.recvfrom(1024)
            except OSError:
                continue

            count += 1
            now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            parsed = parse_push_update(data)
            if parsed:
                print(f"\n  [{now}] PUSH #{count} from {addr[0]}:{addr[1]}")
                print(f"    Serial:    {parsed['serial']}")
                print(f"    SwitchMap: 0x{parsed['switchmap']:08X} ({parsed['switchmap']})")
                print(f"    Status:    0x{parsed['status']:08X} ({parsed['status']})")
                print(f"    Channels:  {format_channel_status(parsed['switchmap'], parsed['status'])}")
            else:
                print(f"\n  [{now}] RAW #{count} from {addr[0]}:{addr[1]}"
                      f" ({len(data)} bytes)")
                print(f"    Hex: {data.hex()}")
                # Try to show as ASCII too
                ascii_repr = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)
                print(f"    ASCII: {ascii_repr}")

    except KeyboardInterrupt:
        print(f"\n\n  Interrupted by user.")

    elapsed = time.monotonic() - start
    print(f"\n  Done. Received {count} packets in {elapsed:.1f}s.")
    sock.close()


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def get_broadcast_address() -> str:
    """Try to determine the broadcast address for the default interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()

        # Assume /24 subnet — derive broadcast
        parts = local_ip.split(".")
        parts[3] = "255"
        broadcast = ".".join(parts)
        print(f"  Auto-detected local IP: {local_ip}")
        print(f"  Using broadcast address: {broadcast} (assuming /24)")
        return broadcast
    except Exception:
        return "255.255.255.255"


def format_channel_status(switchmap: int, status: int) -> str:
    """Format individual channel states from switchmap and status bitmasks."""
    channels = []
    for bit in range(32):
        mask = 1 << bit
        if switchmap & mask:
            state = "ON" if (status & mask) else "OFF"
            channels.append(f"ch{bit}(map={mask})={'ON' if (status & mask) else 'OFF'}")
    return ", ".join(channels) if channels else "none"


def dump_packet(serial: str) -> None:
    """Print packet hex dumps for a given serial (no network)."""
    print(f"[Packet dump for serial '{serial}' — LITTLE-ENDIAN format]\n")

    packets = [
        ("get-status", f"0x{PKT_GET_DIGITAL:04X}", build_get_status_packet(serial)),
        ("set-status ON (map=8)", f"0x{PKT_SET_DIGITAL:04X}",
         build_set_status_packet(serial, 8, 8)),
        ("set-status OFF (map=8)", f"0x{PKT_SET_DIGITAL:04X}",
         build_set_status_packet(serial, 8, 0)),
        ("set-speed (map=8, speed=3)", f"0x{PKT_SET_ANALOG:04X}",
         build_set_speed_packet(serial, 8, 3)),
        ("get-config", f"0x{PKT_GET_CONFIG:04X}", build_get_config_packet(serial)),
    ]

    for name, pkt_type, pkt in packets:
        print(f"  {name} ({pkt_type}) — {len(pkt)} bytes:")
        for i in range(0, len(pkt), 16):
            hex_part = " ".join(f"{b:02x}" for b in pkt[i:i + 16])
            ascii_part = "".join(
                chr(b) if 32 <= b < 127 else "." for b in pkt[i:i + 16]
            )
            print(f"    {i:04x}: {hex_part:<48s} {ascii_part}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Smarteefi UDP Device Scanner & Controller (Little-Endian)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                          # Scan passive + active, 30s
  %(prog)s --passive --duration 60                  # Passive only for 60 seconds
  %(prog)s --active --broadcast 192.168.0.255       # Active probe
  %(prog)s --target se5110000385                    # Get status of a device
  %(prog)s --set se5110000385 8 on                  # Turn channel map=8 ON
  %(prog)s --set se5110000385 8 off                 # Turn channel map=8 OFF
  %(prog)s --set-speed se5110000385 8 3             # Set fan speed to 3
  %(prog)s --set-intensity se5110000385 8 50        # Set light intensity to 50
  %(prog)s --listen-push 60                         # Listen for push updates
  %(prog)s --dump-packet se5110000385               # Print packet hex (no network)
        """,
    )

    mode_group = parser.add_argument_group("Mode selection")
    mode_group.add_argument(
        "--passive", action="store_true",
        help="Passive mode only: listen on port 8890 for push updates",
    )
    mode_group.add_argument(
        "--active", action="store_true",
        help="Active mode only: send probes on port 10201",
    )
    mode_group.add_argument(
        "--target", type=str, metavar="SERIAL",
        help="Targeted get-status for a specific device serial",
    )
    mode_group.add_argument(
        "--set", nargs=3, metavar=("SERIAL", "MAP", "ON_OFF"),
        help="Set-status: SERIAL channel_MAP on|off",
    )
    mode_group.add_argument(
        "--set-speed", nargs=3, metavar=("SERIAL", "MAP", "SPEED"),
        help="Set-speed: SERIAL channel_MAP speed (0-5)",
    )
    mode_group.add_argument(
        "--set-intensity", nargs=3, metavar=("SERIAL", "MAP", "INTENSITY"),
        help="Set-intensity: SERIAL channel_MAP intensity (0-100)",
    )
    mode_group.add_argument(
        "--listen-push", type=float, metavar="SECONDS", default=None,
        help="Listen on port 8890 for push updates (for testing state change events)",
    )
    mode_group.add_argument(
        "--dump-packet", type=str, metavar="SERIAL",
        help="Print raw packet hex for a serial (no network)",
    )

    net_group = parser.add_argument_group("Network options")
    net_group.add_argument(
        "--broadcast", type=str, default=None,
        help="Broadcast address (default: auto-detect, assumes /24)",
    )
    net_group.add_argument(
        "--bind", type=str, default="",
        help="IP address to bind to (default: all interfaces)",
    )
    net_group.add_argument(
        "--duration", type=float, default=30,
        help="Listen duration in seconds (default: 30)",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("  Smarteefi UDP Scanner & Controller (Little-Endian)")
    print("=" * 60)
    print()

    # --- Dump packet mode (no network) ---
    if args.dump_packet:
        dump_packet(args.dump_packet)
        return

    # --- Push update listener ---
    if args.listen_push is not None:
        print(f"[Push Update Listener — port {PUSH_PORT}]\n")
        listen_push_updates(args.listen_push, args.bind)
        return

    # Determine broadcast address for modes that need it
    broadcast = args.broadcast
    needs_broadcast = (args.target or args.set or args.set_speed
                       or args.set_intensity or args.active
                       or (not args.passive))
    if not broadcast and needs_broadcast:
        broadcast = get_broadcast_address()

    # --- Targeted set-status ---
    if args.set:
        serial, map_str, on_off = args.set
        channel_map = int(map_str)
        turn_on = on_off.lower() in ("on", "1", "true", "yes")
        print(f"[Set-Status: {serial} map={channel_map} {'ON' if turn_on else 'OFF'}]\n")
        targeted_set_status(broadcast, serial, channel_map, turn_on, args.bind)
        return

    # --- Targeted set-speed ---
    if args.set_speed:
        serial, map_str, speed_str = args.set_speed
        channel_map = int(map_str)
        speed = int(speed_str)
        print(f"[Set-Speed: {serial} map={channel_map} speed={speed}]\n")
        targeted_set_speed(broadcast, serial, channel_map, speed, args.bind)
        return

    # --- Targeted set-intensity ---
    if args.set_intensity:
        serial, map_str, intensity_str = args.set_intensity
        channel_map = int(map_str)
        intensity = int(intensity_str)
        print(f"[Set-Intensity: {serial} map={channel_map} intensity={intensity}]\n")
        targeted_set_intensity(broadcast, serial, channel_map, intensity, args.bind)
        return

    # --- Targeted get-status ---
    if args.target:
        print(f"[Targeted get-status for '{args.target}']\n")
        result = targeted_get_status(broadcast, args.target, args.bind)
        if result and "switchmap" in result and "statusmap" in result:
            print(f"\n  Channel status:")
            print(f"    {format_channel_status(result['switchmap'], result['statusmap'])}")
        return

    # Default: both modes if neither specified
    run_passive = args.passive or (not args.passive and not args.active)
    run_active = args.active or (not args.passive and not args.active)

    all_devices = OrderedDict()

    # --- Passive discovery ---
    if run_passive:
        print(f"[Phase 1: Passive Discovery — port {PUSH_PORT}]")
        print()
        passive_devices = passive_discovery(args.duration, args.bind)
        for serial, info in passive_devices.items():
            info["discovery_method"] = "passive (push update)"
            all_devices[serial] = info
        print()

    # --- Active discovery ---
    if run_active:
        print(f"[Phase 2: Active Discovery — port {CONTROL_PORT}]")
        print(f"  Broadcast: {broadcast}")
        print()
        active_duration = min(args.duration, 15)
        active_devices = active_discovery(broadcast, active_duration, args.bind)
        for serial, info in active_devices.items():
            if serial in all_devices:
                all_devices[serial]["also_found_active"] = True
                all_devices[serial].update(
                    {k: v for k, v in info.items() if k not in all_devices[serial]}
                )
            else:
                info["discovery_method"] = "active (probe response)"
                all_devices[serial] = info
        print()

    # --- Summary ---
    print("=" * 60)
    print(f"  SCAN COMPLETE — {len(all_devices)} device(s) found")
    print("=" * 60)

    if not all_devices:
        print()
        print("  No devices discovered. Troubleshooting tips:")
        print("  - Ensure Smarteefi devices are powered on and connected to WiFi")
        print("  - Ensure you're on the same network/subnet as the devices")
        print("  - Try increasing --duration (e.g., --duration 120)")
        print("  - For passive mode, ensure port 8890 is not blocked by firewall")
        print("  - For passive mode, try running with sudo for port binding")
        print("  - If you know a device serial, try: --target YOUR_SERIAL")
        print()
    else:
        print()
        for i, (serial, info) in enumerate(all_devices.items(), 1):
            print(f"  Device {i}: {serial}")
            print(f"    Source IP:    {info.get('source_ip', 'unknown')}")
            print(f"    Method:      {info.get('discovery_method', 'unknown')}")

            smap = info.get("switchmap")
            status = info.get("status") or info.get("statusmap")
            if smap is not None:
                print(f"    SwitchMap:   {smap} (0x{smap:08X})")
            if status is not None:
                print(f"    Status:      {status} (0x{status:08X})")
            if smap is not None and status is not None:
                print(f"    Channels:    {format_channel_status(smap, status)}")

            # Generate cloudid for reference
            crc = zlib.crc32(serial.encode())
            cloudid = crc ^ 4102444800  # XOR with Jan 1, 2099 timestamp
            cloudid = cloudid & 0xFFFFFFFF
            print(f"    CloudID:     {cloudid}")

            if info.get("first_seen"):
                print(f"    First seen:  {info['first_seen']}")
            if info.get("last_seen"):
                print(f"    Last seen:   {info['last_seen']}")
            if info.get("update_count"):
                print(f"    Updates:     {info['update_count']}")
            print()

    # Also dump as JSON for programmatic use
    if all_devices:
        print("  [JSON output]")
        print(json.dumps(list(all_devices.values()), indent=2))
        print()


if __name__ == "__main__":
    main()
