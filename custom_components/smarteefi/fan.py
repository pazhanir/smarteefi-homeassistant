import logging
import os
import asyncio
import math

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.util.percentage import ranged_value_to_percentage, percentage_to_ranged_value
from homeassistant.util.scaling import int_states_in_range
from .const import DOMAIN, ARCH, OS

SPEED_RANGE = (1, 4)

_LOGGER = logging.getLogger(__name__)

_LOGGER.info(f"Domain is {DOMAIN}")

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi fans from a config entry."""
    
    # Get correct integration path
    INTEGRATION_PATH = hass.config.path(f"custom_components/smarteefi")
    
    # Full path to HACLI binary
    if OS == 'win':
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
    else:
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")
    
    _LOGGER.debug(f"Using HACLI path: {HACLI}")

    devices = entry.data.get("devices", [])

    if not devices:
        _LOGGER.error("No devices found for Smarteefi fan.")
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

    # Pass network_interface to SmarteefiFan constructor
    fans = [SmarteefiFan(device, HACLI, ip_address, netmask) for device in devices if device["type"] == "fan"]
    async_add_entities(fans, True)

class SmarteefiFan(FanEntity):
    """Representation of a Smarteefi fan."""

    def __init__(self, device, hacli_path, ip_address, netmask):
        self._device = device
        self._state = False  # Assume the fan is off by default
        self._name = device.get("name", "Unnamed Fan")  # Set name properly
        self._unique_id = device.get("id", "")
        self._cloud_id = device.get("cloudid", "")
        self._hacli = hacli_path
        self.ip_address = ip_address
        self.netmask = netmask  
        self._percentage = None
        self._speed_count = 4
        self._speed = 0
     
    @property
    def name(self):
        """Return the name of the fan."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the fan."""
        return self._unique_id
    
    @property
    def supported_features(self):
        """supported features."""
        return FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF | FanEntityFeature.SET_SPEED

    @property
    def is_on(self) -> bool | None:
        """Return true if device is on."""
        return self._state

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage."""
        return ranged_value_to_percentage(SPEED_RANGE, self._speed)
 
    @property
    def speed_count(self) -> int:
        """Return the number of speeds the fan supports."""
        return int_states_in_range(SPEED_RANGE)
    
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

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs) -> None:
        """Turn the fan on."""
        _LOGGER.info("Turning on Smarteefi fan: %s perentage", self._name, percentage)
        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "1"]):
            self._state = True
            if percentage:
                await self.async_set_percentage(percentage)            
            self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the fan off."""
        _LOGGER.info("Turning off Smarteefi fan: %s", self._name)
        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]):
            self._state = False
            self.schedule_update_ha_state()

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed of the fan."""
        speed = value_in_range = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))        
        _LOGGER.info("Setting speed of Smarteefi fan: %s to %d", self._name, speed)
        if speed:
            if await self._execute_cli([self.ip_address, self.netmask, "set-speed", self._unique_id, str(self._cloud_id), str(speed)]):
                self._speed = speed
                if speed:
                    self._state = True
                else:
                    self._state = False
                self.schedule_update_ha_state()
        else:
            if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]):
                self._state = False
                self.schedule_update_ha_state()
