import logging
import os
import asyncio
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
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
        self._state = False
        self._name = device.get("name", "Unnamed Switch")
        self._unique_id = device.get("id", "")  # Format: "serial:ignored:smap"
        self._cloud_id = device.get("cloudid", "")
        self._hacli = hacli_path
        self.ip_address = ip_address
        self.netmask = netmask
        self._update_unsub = None
        self._smap = None  # Store smap value from entity ID
        self._available = True

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
        """Update state from dispatcher."""
        self._available = data.get('available', True)
        
        if not self._available:
            self.schedule_update_ha_state()
            return

        if "status" in data and "smap" in data:
            received_smap = data["smap"]
            status = data["status"]
            
            if received_smap == self._smap:
                new_state = (status != 0) and (received_smap & status)
                if self._state != new_state:
                    self._state = new_state
                    _LOGGER.debug(
                        f"Updated switch {self._name} - "
                        f"State: {'on' if new_state else 'off'}, "
                        f"Smap: {received_smap}, Status: {status}"
                    )
        
        self.schedule_update_ha_state()

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

    @property
    def available(self):
        """Return True if the entity is available."""
        return self._available

    async def _execute_cli(self, command):
        """Run the HACLI binary with the given command."""
        full_command = [self._hacli] + command

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