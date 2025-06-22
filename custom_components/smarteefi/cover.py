import logging
import os
import asyncio

from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,  # Use CoverEntityFeature instead of SUPPORT_* constants
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, ARCH, OS

_LOGGER = logging.getLogger(__name__)

_LOGGER.info(f"Domain is {DOMAIN}")

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi covers from a config entry."""
    
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
        _LOGGER.error("No devices found for Smarteefi cover.")
        return
    
    # Get host IP address from config_entry
    ip_address = entry.data.get("ip_address")

    if not ip_address:
        _LOGGER.error("ip_address not found in config entry!")
        return

    # Get netmask from config_entry
    netmask = entry.data.get("netmask")

    if not netmask:
        _LOGGER.error("netmask not found in config entry!")
        return            

    # Pass network_interface to SmarteefiCover constructor
    covers = [SmarteefiCover(device, HACLI, ip_address, netmask) for device in devices if device["type"] == "cover"]
    async_add_entities(covers, True)

class SmarteefiCover(CoverEntity):
    """Representation of a Smarteefi cover."""

    def __init__(self, device, hacli_path, ip_address, netmask):
        self._device = device
        self._state = "closed"  # Assume the cover is closed by default
        self._name = device.get("name", "Unnamed Cover")  # Set name properly
        self._unique_id = device.get("id", "")
        self._cloud_id = device.get("cloudid", "")
        self._hacli = hacli_path
        self.ip_address = ip_address
        self.netmask = netmask
        self._current_position = 0  # Assume the cover is fully closed initially
        self._update_unsub = None
        self._smap = None  # Store smap value from entity ID
        self._attr_available = True

        # Extract serial:smap from unique_id (format: "serial:ignored:smap")
        parts = self._unique_id.split(':')
        if len(parts) == 3:
            self._entity_match_id = f"{parts[0]}:{parts[2]}"  # serial:smap
            self._smap = int(parts[2])
        else:
            _LOGGER.error(f"Invalid unique_id format: {self._unique_id}")

    async def async_added_to_hass(self):
        """Register update dispatcher."""
        if hasattr(self, '_entity_match_id'):
            self._update_unsub = async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_device_update_{self._entity_match_id}",
                self._handle_device_update
            )

    async def async_will_remove_from_hass(self):
        """Unregister update dispatcher."""
        if self._update_unsub:
            self._update_unsub()

    def _handle_device_update(self, data):
        """Update state from coordinator or UDP message."""
        # Update availability if provided by the coordinator
        if "available" in data:
            self._attr_available = data["available"]

        # Update state if status is provided (from coordinator or UDP)
        if "status" in data:
            # If a status update is received, the device is considered available.
            self._attr_available = True
            
            received_smap = data["smap"]
            status = data["status"]
            
            # Only process if smap matches our entity's smap
            if received_smap != self._smap:
                return
            
            if status == self._smap:
                self._state = "open"
                self._current_position = 100
            else:
                self._state = "closed"
                self._current_position = 0

            _LOGGER.debug(
                f"Updated cover {self._name} - "
                f"State: {'Opened' if status else 'Closed'}, "
                f"Current Position: {self._current_position}, Status: {status}"
            )
        
        # Schedule an update in Home Assistant
        self.schedule_update_ha_state()

    @property
    def name(self):
        """Return the name of the cover."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the cover."""
        return self._unique_id

    @property
    def is_closed(self):
        """Return True if the cover is closed."""
        return self._current_position == 0

    @property
    def current_cover_position(self):
        """Return the current position of the cover."""
        return self._current_position

    @property
    def supported_features(self):
        """Flag supported features."""
        return CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

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

    async def async_open_cover(self, **kwargs):
        """Open the cover."""
        _LOGGER.info("Opening Smarteefi cover: %s", self._name)
        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "1"]):
            self._current_position = 100
            self._state = "open"
            self.schedule_update_ha_state()

    async def async_close_cover(self, **kwargs):
        """Close the cover."""
        _LOGGER.info("Closing Smarteefi cover: %s", self._name)
        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]):
            self._current_position = 0
            self._state = "closed"
            self.schedule_update_ha_state()

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        position = kwargs.get("position", 0)
        _LOGGER.info(f"Setting Smarteefi cover position to {position}: {self._name}")

        prev_position = self._current_position

        """Determine the direction."""
        if position == 0:
            status = "0"
            percentage = "0"
        elif position == 100:
            status = "1"
            percentage = "0"                     
        elif position > prev_position:
            status = "1"
            percentage = str(position - prev_position)
        elif prev_position > position:
            status = "0"
            percentage = str(prev_position - position) 
        else:
            self._current_position = position
            if position == 0:
                self._state = "closed"
            elif position == 100:
                self._state = "open"
            else:
                self._state = "partially_open"
            self.schedule_update_ha_state()

        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), status, percentage]):
            self._current_position = position
            if position == 0:
                self._state = "closed"
            elif position == 100:
                self._state = "open"
            else:
                self._state = "partially_open"
            self.schedule_update_ha_state()
