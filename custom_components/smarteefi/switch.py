import logging
import os
import asyncio

from homeassistant.components.switch import SwitchEntity
from .const import DOMAIN, ARCH, OS

_LOGGER = logging.getLogger(__name__)

_LOGGER.info(f"Domain is {DOMAIN}")

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi switches from a config entry."""
    
    # Get correct integration path
    INTEGRATION_PATH = hass.config.path(f"custom_components/smarteefi")
    
    # Full path to HACLI binary
    if(OS=='win'):
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
    else:
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")
    
    _LOGGER.debug(f"Using HACLI path: {HACLI}")

    devices = entry.data.get("devices", [])

    if not devices:
        _LOGGER.error("No devices found for Smarteefi switch.")
        return
    
    # Get host ip address from config_entry
    ip_address = entry.data.get("ip_address")

    if not ip_address:
        _LOGGER.error("ip_address not found in config entry!")
        return

    # Get host ip address from config_entry
    netmask = entry.data.get("netmask")

    if not netmask:
        _LOGGER.error("netmask not found in config entry!")
        return            

    # Pass network_interface to SmarteefiSwitch constructor
    switches = [SmarteefiSwitch(device, HACLI, ip_address, netmask) for device in devices if device["type"] == "switch"]
    async_add_entities(switches, True)

class SmarteefiSwitch(SwitchEntity):
    """Representation of a Smarteefi switch."""

    def __init__(self, device, hacli_path, ip_address, netmask):
        self._device = device
        self._state = False  # Assume the switch is off by default
        self._name = device.get("name", "Unnamed Switch")  # Set name properly
        self._unique_id = device.get("id", "")
        self._cloud_id = device.get("cloudid", "")
        self._hacli = hacli_path
        self.ip_address = ip_address
        self.netmask = netmask  
     
    @property
    def name(self):
        """Return the name of the switch."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the switch."""
        return self._unique_id

    @property
    def is_on(self):
        """Return True if the switch is on."""
        return self._state

    async def _execute_cli(self, command):
        """Run the HACLI binary with the given command."""
        full_command = [self._hacli] + command  # Prepend HACLI path

        _LOGGER.debug(f"Executing CLI command: {' '.join(full_command)}")

        try:
            process = await asyncio.create_subprocess_exec(
                self._hacli, *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                _LOGGER.debug(f"Command succeeded: {' '.join(full_command)}, Output: {stdout.decode().strip()}")
                return True
            else:
                _LOGGER.error(f"Command failed: {' '.join(full_command)}, Error: {stderr.decode().strip()}")
                return False
        except FileNotFoundError:
            _LOGGER.error(f"CLI binary not found at {self._hacli}")
            return False
        except Exception as e:
            _LOGGER.error(f"Error executing CLI: {e}")
            return False

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        _LOGGER.info("Turning on Smarteefi switch: %s", self._name)
        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "1"]):
            self._state = True
            self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        _LOGGER.info("Turning off Smarteefi switch: %s", self._name)
        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]):
            self._state = False
            self.schedule_update_ha_state()