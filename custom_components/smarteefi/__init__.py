"""Smarteefi integration — pure Python UDP control with HA DataUpdateCoordinator."""

import logging
import asyncio
import socket
import time
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, SYNC_INTERVAL, INITIAL_SYNC_INTERVAL, API_LOGIN_URL, API_DEVICES_URL
from . import udp_protocol

_LOGGER = logging.getLogger(__name__)

# How long (seconds) after a command before we skip poll updates for that serial.
# This prevents a stale poll from overwriting a fresh command response.
COMMAND_STALENESS_WINDOW = 3.0

# Minimum delay (seconds) between consecutive UDP commands to the same device module.
# ESP32 devices drop commands that arrive too quickly after the previous one.
# This fixes the combined switch bug where toggling a group sends two commands
# back-to-back and the device only processes the first one.
INTER_COMMAND_DELAY = 0.2

PLATFORMS = ["switch", "fan", "light", "cover"]


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Smarteefi integration."""
    hass.data.setdefault(DOMAIN, {})

    async def handle_refresh_devices(call):
        """Handle the service call to refresh devices."""
        entries = hass.config_entries.async_entries(DOMAIN)
        if entries:
            await async_refresh_devices(hass, entries[0])

    async def handle_sync_states(call):
        """Handle the service call to sync states."""
        coordinator = hass.data.get(DOMAIN, {}).get("coordinator")
        if coordinator:
            await coordinator.async_request_refresh()

    hass.services.async_register(DOMAIN, "discover_devices", handle_refresh_devices)
    hass.services.async_register(DOMAIN, "sync_states", handle_sync_states)
    return True


# ---------------------------------------------------------------------------
# DataUpdateCoordinator — hybrid polling + push
# ---------------------------------------------------------------------------

class SmarteefiCoordinator(DataUpdateCoordinator):
    """Smarteefi data update coordinator.

    Polls all modules via UDP get-status on an interval (5s initially, then 5s regular).
    Push updates from port 8890 are merged in via async_set_updated_data().
    Inter-command delays prevent devices from dropping back-to-back commands.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, broadcast_addr: str):
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=INITIAL_SYNC_INTERVAL),
        )
        self.entry = entry
        self.broadcast_addr = broadcast_addr
        self._is_initial_sync = True

        # ESP32 fallback settings from config entry
        self._fallback_enabled = entry.data.get("fallback_enabled", False)
        self._fallback_ip = entry.data.get("fallback_ip", "")

        # Per-serial asyncio locks to serialize commands to the same device module.
        # This prevents race conditions when multiple channels on the same module
        # are toggled in quick succession.
        self._serial_locks: dict[str, asyncio.Lock] = {}

        # Track last command time per serial so polling can skip stale updates.
        # Key: serial, Value: monotonic timestamp of last successful command response.
        self._last_command_time: dict[str, float] = {}

        # Build module map: serial -> combined switchmap for get-status
        self._modules: dict[str, int] = {}
        for device in entry.data.get("devices", []):
            parts = device["id"].split(":")
            serial = parts[0]
            smap = int(parts[2])
            if serial in self._modules:
                self._modules[serial] |= smap
            else:
                self._modules[serial] = smap

        # Pre-create locks for known modules
        for serial in self._modules:
            self._serial_locks[serial] = asyncio.Lock()

        _LOGGER.debug(
            "Coordinator init: broadcast=%s, modules=%s, fallback=%s (ip=%s)",
            broadcast_addr,
            {s: f"0x{m:X}" for s, m in self._modules.items()},
            self._fallback_enabled,
            self._fallback_ip,
        )

    def get_serial_lock(self, serial: str) -> asyncio.Lock:
        """Get or create the asyncio Lock for a given module serial."""
        if serial not in self._serial_locks:
            self._serial_locks[serial] = asyncio.Lock()
        return self._serial_locks[serial]

    def mark_command_time(self, serial: str) -> None:
        """Record that a command response was just processed for this serial."""
        self._last_command_time[serial] = time.monotonic()

    def _is_recently_commanded(self, serial: str) -> bool:
        """Check if a command was processed recently for this serial."""
        last_time = self._last_command_time.get(serial)
        if last_time is None:
            return False
        return (time.monotonic() - last_time) < COMMAND_STALENESS_WINDOW

    async def ensure_command_gap(self, serial: str) -> None:
        """Ensure a minimum inter-command delay for a device module.

        Call this at the start of every locked command section. If another
        command was sent to the same serial within INTER_COMMAND_DELAY seconds,
        this sleeps for the remaining time. Single commands have zero delay;
        only back-to-back commands are throttled.
        """
        last_time = self._last_command_time.get(serial)
        if last_time is not None:
            elapsed = time.monotonic() - last_time
            remaining = INTER_COMMAND_DELAY - elapsed
            if remaining > 0:
                _LOGGER.debug(
                    "Inter-command delay for %s: sleeping %.0fms",
                    serial, remaining * 1000,
                )
                await asyncio.sleep(remaining)

    async def _async_update_data(self) -> dict:
        """Poll all modules via UDP get-status with retry + ESP32 fallback."""

        # Switch from initial fast interval to regular interval after first poll
        if self._is_initial_sync:
            self._is_initial_sync = False
            self.update_interval = timedelta(seconds=SYNC_INTERVAL)
            _LOGGER.debug("Switching to regular sync interval (%ds)", SYNC_INTERVAL)

        data: dict = dict(self.data) if self.data else {}

        for serial, combined_switchmap in self._modules.items():
            # Skip polling if a command was just processed for this serial.
            # The command response already has the freshest state; polling now
            # risks overwriting it with stale data if the device hasn't fully
            # settled yet.
            if self._is_recently_commanded(serial):
                _LOGGER.debug(
                    "Skipping poll for %s — command processed recently (%.1fs ago)",
                    serial,
                    time.monotonic() - self._last_command_time.get(serial, 0),
                )
                continue

            _LOGGER.debug("Polling %s (switchmap=0x%X)", serial, combined_switchmap)

            # --- First attempt ---
            resp = await udp_protocol.async_get_status(
                serial, self.broadcast_addr, combined_switchmap
            )

            if resp is None or resp.get("result") != 1:
                _LOGGER.warning(
                    "Device %s offline on first attempt, retrying in 5s...", serial
                )
                await asyncio.sleep(5)

                # --- Second attempt ---
                resp = await udp_protocol.async_get_status(
                    serial, self.broadcast_addr, combined_switchmap
                )

            if resp is not None and resp.get("result") == 1:
                # Success
                _LOGGER.debug(
                    "Device %s OK: switchmap=0x%X, statusmap=0x%X",
                    serial,
                    resp.get("switchmap", 0),
                    resp.get("statusmap", 0),
                )
                data[serial] = {
                    "statusmap": resp.get("statusmap", 0),
                    "switchmap": resp.get("switchmap", 0),
                    "available": True,
                }
            else:
                # Both attempts failed — ESP32 fallback check (if enabled)
                if self._fallback_enabled and self._fallback_ip:
                    esp32_reachable = await self._check_esp32_fallback()
                    if esp32_reachable:
                        _LOGGER.debug(
                            "Device %s UDP offline, but ESP32 (%s) reachable. "
                            "Keeping previous state.",
                            serial,
                            self._fallback_ip,
                        )
                        # Keep whatever was in data[serial] before (don't overwrite)
                        continue

                _LOGGER.error(
                    "Device %s offline after retry%s. Marking unavailable.",
                    serial,
                    " + ESP32 check" if self._fallback_enabled else "",
                )
                data[serial] = {**data.get(serial, {}), "available": False}

        return data

    async def _check_esp32_fallback(self) -> bool:
        """Check if ESP32 webserver is reachable at the configured fallback IP."""
        if not self._fallback_ip:
            return False
        try:
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(5):
                async with session.get(f"http://{self._fallback_ip}") as response:
                    return response.status == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Push listener on port 8890
# ---------------------------------------------------------------------------

class SmarteefiPushProtocol(asyncio.DatagramProtocol):
    """UDP listener on port 8890 for device push updates."""

    def __init__(self, coordinator: SmarteefiCoordinator):
        """Initialize the push listener."""
        self.coordinator = coordinator
        self.transport = None

    def connection_made(self, transport):
        """Configure the socket when connection is made."""
        self.transport = transport
        sock = transport.get_extra_info("socket")
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass  # Not available on all platforms
            _LOGGER.debug("Push listener socket configured on port %d", udp_protocol.PUSH_PORT)
        except Exception as e:
            _LOGGER.error("Error configuring push listener socket: %s", e)

    def datagram_received(self, data, addr):
        """Handle incoming push updates from devices."""
        _LOGGER.debug("Push update from %s (%d bytes): %s", addr, len(data), data.hex())

        parsed = None

        # Try standard 66-byte response format first (0xAAAA preamble)
        if len(data) >= 58 and data[0:2] == b"\xaa\xaa":
            resp = udp_protocol.parse_response(data)
            if resp and resp.get("serial"):
                parsed = {
                    "serial": resp["serial"],
                    "statusmap": resp.get("statusmap", 0),
                    "switchmap": resp.get("switchmap", 0),
                }

        # Try 26-byte push update format
        if parsed is None:
            push = udp_protocol.parse_push_update(data)
            if push:
                parsed = {
                    "serial": push["serial"],
                    "statusmap": push["status"],
                    "switchmap": push.get("switchmap", 0),
                }

        if parsed is None:
            _LOGGER.warning("Could not parse push update from %s (%d bytes)", addr, len(data))
            return

        serial = parsed["serial"]
        _LOGGER.info(
            "Push update for %s: statusmap=0x%X, switchmap=0x%X",
            serial,
            parsed["statusmap"],
            parsed.get("switchmap", 0),
        )

        # Merge into coordinator data and notify all entities
        current_data = dict(self.coordinator.data) if self.coordinator.data else {}
        current_data[serial] = {
            "statusmap": parsed["statusmap"],
            "switchmap": parsed.get(
                "switchmap",
                current_data.get(serial, {}).get("switchmap", 0),
            ),
            "available": True,
        }
        self.coordinator.async_set_updated_data(current_data)


# ---------------------------------------------------------------------------
# Entry setup / unload
# ---------------------------------------------------------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Smarteefi integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    access_token = entry.data.get("access_token")
    if not access_token:
        _LOGGER.error("Access token is missing in config entry")
        return False

    # Auto-detect network interface, IP, and netmask
    network_interface, ip_address, netmask = _detect_network()
    if not ip_address or not netmask:
        _LOGGER.error("Unable to determine IP address or netmask")
        return False

    _LOGGER.debug(
        "Detected interface: %s, IP: %s, Netmask: %s",
        network_interface, ip_address, netmask,
    )

    # Store network info in config entry for reference
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "network_interface": network_interface,
            "ip_address": ip_address,
            "netmask": netmask,
        },
    )

    broadcast_addr = udp_protocol.compute_broadcast_addr(ip_address, netmask)
    _LOGGER.info("Broadcast address: %s", broadcast_addr)

    # Create the coordinator
    coordinator = SmarteefiCoordinator(hass, entry, broadcast_addr)

    # Start push listener on port 8890
    push_transport = None
    try:
        loop = asyncio.get_running_loop()
        push_transport, _ = await loop.create_datagram_endpoint(
            lambda: SmarteefiPushProtocol(coordinator),
            local_addr=("0.0.0.0", udp_protocol.PUSH_PORT),
        )
        _LOGGER.info("Push listener started on port %d", udp_protocol.PUSH_PORT)
    except Exception as e:
        _LOGGER.warning(
            "Failed to start push listener on port %d: %s. "
            "Continuing without real-time push updates.",
            udp_protocol.PUSH_PORT,
            e,
        )

    # Do first data refresh (blocks briefly but ensures entities have data)
    await coordinator.async_refresh()

    # Store coordinator and transport in hass.data
    hass.data[DOMAIN]["coordinator"] = coordinator
    hass.data[DOMAIN]["push_transport"] = push_transport

    # Forward setup to all entity platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Close push listener
    push_transport = hass.data[DOMAIN].get("push_transport")
    if push_transport:
        push_transport.close()
        _LOGGER.info("Push listener stopped")

    # Unload all entity platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop("coordinator", None)
        hass.data[DOMAIN].pop("push_transport", None)

    return unload_ok


# ---------------------------------------------------------------------------
# Network detection
# ---------------------------------------------------------------------------

def _detect_network():
    """Detect the active network interface, IP address, and netmask."""
    try:
        # Get local IP via socket trick (connects to Google DNS, doesn't send data)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]

        # Try psutil for interface name and netmask (available in HA environment)
        try:
            import psutil
            for iface, addrs in psutil.net_if_addrs().items():
                if iface in ("lo", "lo0"):
                    continue
                for addr in addrs:
                    if addr.family == socket.AF_INET and addr.address == local_ip:
                        return iface, addr.address, addr.netmask
        except ImportError:
            _LOGGER.debug("psutil not available, using fallback netmask")

        # Fallback: assume /24 subnet (covers most home networks)
        return "unknown", local_ip, "255.255.255.0"

    except Exception as e:
        _LOGGER.error("Error detecting network: %s", e)
        return None, None, None


# ---------------------------------------------------------------------------
# Device fetching / refresh (v3 REST API)
# ---------------------------------------------------------------------------

async def fetch_devices(session: aiohttp.ClientSession, access_token: str):
    """Fetch devices from Smarteefi v3 API and transform to existing format."""
    payload = {"UserDevice": {"access_token": access_token}}
    headers = {"Content-Type": "application/json"}

    async with session.post(API_DEVICES_URL, json=payload, headers=headers) as response:
        response_text = await response.text()

        if response.status != 200:
            _LOGGER.error(
                "Failed to fetch devices, status: %s, response: %s",
                response.status,
                response_text,
            )
            return []

        json_response = await response.json()
        if json_response.get("result") != "success":
            _LOGGER.error("API returned failure: %s", json_response)
            return []

        # Transform v3 switches[] to device format
        switches = json_response.get("switches", [])
        devices = []
        for sw in switches:
            device_id = f"{sw['serial']}:{sw['group_id']}:{int(sw['map'])}"
            devices.append({
                "id": device_id,
                "type": "switch",  # Default type; config_flow lets user override
                "name": sw.get("name", sw["serial"]),
            })
        return devices


async def async_refresh_devices(hass: HomeAssistant, entry: ConfigEntry):
    """Refresh devices from Smarteefi v3 API, with re-login if token is stale."""
    session = async_get_clientsession(hass)
    access_token = entry.data.get("access_token")

    if not access_token:
        _LOGGER.error("Access token is missing in config entry")
        return False

    try:
        _LOGGER.debug("Refreshing devices using access token")
        devices = await fetch_devices(session, access_token)

        # If fetch returned empty, try re-login with stored credentials
        if not devices:
            _LOGGER.warning("Device fetch returned empty, attempting re-login")
            email = entry.data.get("email")
            password = entry.data.get("password")
            if email and password:
                new_token = await _api_relogin(session, email, password)
                if new_token:
                    access_token = new_token
                    hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, "access_token": access_token},
                    )
                    devices = await fetch_devices(session, access_token)

        _LOGGER.debug("Devices refreshed: %s", devices)

        # Preserve user-selected types for existing devices
        current_devices = entry.data.get("devices", [])
        current_type_map = {d["id"]: d["type"] for d in current_devices}
        for device in devices:
            if device["id"] in current_type_map:
                device["type"] = current_type_map[device["id"]]

        # Identify removed devices
        current_device_ids = {d["id"] for d in current_devices}
        new_device_ids = {d["id"] for d in devices}
        removed_device_ids = current_device_ids - new_device_ids

        # Update config entry with refreshed devices
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "devices": devices},
        )

        # Remove entities for devices no longer present
        entity_registry = er.async_get(hass)
        for entity_entry in entity_registry.entities.values():
            if (
                entity_entry.config_entry_id == entry.entry_id
                and entity_entry.unique_id in removed_device_ids
            ):
                entity_registry.async_remove(entity_entry.entity_id)

        _LOGGER.info("Smarteefi devices refreshed: %s", devices)

        # Reload platforms to reflect changes
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    except Exception as e:
        _LOGGER.error("Error refreshing Smarteefi devices: %s", e)
        return False


async def _api_relogin(session: aiohttp.ClientSession, email: str, password: str):
    """Re-login to Smarteefi v3 API and return new access_token, or None."""
    payload = {
        "LoginForm": {"email": email, "password": password, "app": "smarteefi"}
    }
    headers = {"Content-Type": "application/json"}

    try:
        async with session.post(API_LOGIN_URL, json=payload, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                if data.get("result") == "success":
                    _LOGGER.info("Re-login successful, new access token obtained")
                    return data.get("access_token")
                _LOGGER.error("Re-login failed: %s", data)
            else:
                _LOGGER.error("Re-login API returned status %s", response.status)
    except Exception as e:
        _LOGGER.error("Exception during re-login: %s", e)
    return None
