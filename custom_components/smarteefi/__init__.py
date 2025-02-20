import logging
import aiohttp
import psutil
import socket
import os
import stat
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN, ARCH, OS

_LOGGER = logging.getLogger(__name__)

API_URL = "https://www.smarteefi.com/api/homeassistant_v1/user/devices"

async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Smarteefi integration."""
    _LOGGER.debug("Setting up Smarteefi integration")
    hass.data.setdefault(DOMAIN, {})

    # Get correct integration path
    INTEGRATION_PATH = hass.config.path(f"custom_components/smarteefi")
    
    # Full path to HACLI binary
    if(OS=='win'):
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
    else:
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")
    
    set_executable_permissions(HACLI)
    _LOGGER.debug(f"Using HACLI path: {HACLI}")    

    async def handle_refresh_devices(call):
        """Handle the service call to refresh devices."""
        entry = hass.config_entries.async_entries(DOMAIN)[0]
        await async_refresh_devices(hass, entry)

    hass.services.async_register(DOMAIN, "discover_devices", handle_refresh_devices)
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Smarteefi integration from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    apitoken = entry.data.get("apitoken")
    
    if not apitoken:
        _LOGGER.error("API token is missing in config entry")
        return False

    # Auto-detect the active interface, IP address, and netmask
    network_interface, ip_address, netmask = _get_active_interface_ip_and_netmask()
    if not network_interface or not ip_address or not netmask:
        _LOGGER.error("Unable to determine active network interface, IP address, or netmask")
        return False

    _LOGGER.debug(f"Detected active interface: {network_interface}, IP: {ip_address}, Netmask: {netmask}")

    # Update the config entry with the latest interface details
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "network_interface": network_interface, "ip_address": ip_address, "netmask": netmask}
    )

    session = async_get_clientsession(hass)

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    # Check if devices are already loaded in config_entry
    if "devices" not in entry.data:
        try:
            _LOGGER.debug("Fetching devices using API token: %s", apitoken)
            devices = await fetch_devices(session, apitoken)
            _LOGGER.debug("Devices fetched successfully: %s", devices)

            # Update config_entry with fetched devices
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, "devices": devices}
            )

            _LOGGER.info("Smarteefi devices discovered: %s", devices)

            # Forward setup to all platforms at once
            await hass.config_entries.async_forward_entry_setups(entry, ["switch", "fan", "light", "cover"])

        except Exception as e:
            _LOGGER.error("Error fetching Smarteefi devices: %s", e)
            return False
    else:
        # Devices are already in config_entry, no need to fetch
        await hass.config_entries.async_forward_entry_setups(entry, ["switch", "fan", "light", "cover"])
    
    return True

def _get_interface_ip_and_netmask(interface):
    """Get the IP address and netmask for the specified interface."""
    try:
        addrs = psutil.net_if_addrs().get(interface, [])
        for addr in addrs:
            if addr.family == socket.AF_INET:  # Use socket.AF_INET for IPv4 addresses
                return addr.address, addr.netmask
        _LOGGER.warning(f"No IPv4 address found for interface: {interface}")
        return None, None
    except Exception as e:
        _LOGGER.error(f"Error retrieving IP address and netmask for interface {interface}: {e}")
        return None, None

def _get_active_interface_ip_and_netmask():
    """Determine the active interface with the default gateway and return its name, IP, and netmask."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]

        interfaces = psutil.net_if_addrs()
        for iface, addrs in interfaces.items():
            if iface in ['lo', 'lo0']:  # Skip loopback
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET and addr.address == local_ip:
                    return iface, addr.address, addr.netmask

        _LOGGER.warning("No active interface found matching the default gateway IP.")
        return None, None, None
    except Exception as e:
        _LOGGER.error(f"Error determining active interface: {e}")
        return None, None, None

async def fetch_devices(session: aiohttp.ClientSession, apitoken: str):
    """Fetch devices from Smarteefi API using POST request."""
    payload = {"UserDevice": {"hatoken": apitoken}}
    headers = {"Content-Type": "application/json"}

    async with session.post(API_URL, json=payload, headers=headers) as response:
        response_text = await response.text()

        if response.status != 200:
            _LOGGER.error("Failed to fetch devices, status: %s, response: %s", response.status, response_text)
            return []

        json_response = await response.json()
        if json_response.get("result") != "success":
            _LOGGER.error("API returned failure: %s", json_response)
            return []

        return json_response.get("devices", [])

async def async_refresh_devices(hass: HomeAssistant, entry: ConfigEntry):
    """Refresh devices from Smarteefi API."""
    session = async_get_clientsession(hass)
    apitoken = entry.data.get("apitoken")

    if not apitoken:
        _LOGGER.error("API token is missing in config entry")
        return False

    try:
        _LOGGER.debug("Refreshing devices using API token: %s", apitoken)
        devices = await fetch_devices(session, apitoken)
        _LOGGER.debug("Devices refreshed successfully: %s", devices)

        # Get the current devices from the config entry
        current_devices = entry.data.get("devices", [])
        current_device_ids = {device['id'] for device in current_devices}
        new_device_ids = {device['id'] for device in devices}

        # Identify removed and added devices
        removed_device_ids = current_device_ids - new_device_ids
        added_device_ids = new_device_ids - current_device_ids

        # Update config_entry with refreshed devices
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, "devices": devices}
        )

        # Remove entities for devices that are no longer present
        entity_registry = er.async_get(hass)
        entities_to_remove = [
            entity_entry.entity_id
            for entity_entry in entity_registry.entities.values()
            if entity_entry.config_entry_id == entry.entry_id and entity_entry.unique_id in removed_device_ids
        ]

        for entity_id in entities_to_remove:
            entity_registry.async_remove(entity_id)

        _LOGGER.info("Smarteefi devices refreshed: %s", devices)

        # Reload platforms to reflect new devices
        await hass.config_entries.async_forward_entry_unload(entry, ["switch", "fan", "light", "cover"])
        await hass.config_entries.async_forward_entry_setups(entry, ["switch", "fan", "light", "cover"])

    except Exception as e:
        _LOGGER.error("Error refreshing Smarteefi devices: %s", e)
        return False

def set_executable_permissions(file_path: str) -> None:
    """Set executable permissions on the specified file."""
    if os.path.exists(file_path):
        # Get current permissions
        current_perms = os.stat(file_path).st_mode
        # Add execute permission for owner, group, and others (equivalent to chmod +x)
        os.chmod(file_path, current_perms | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        # Log an error if the file isn’t found (optional, requires hass.logger)
        _LOGGER(f"CLI not found at {file_path}")
