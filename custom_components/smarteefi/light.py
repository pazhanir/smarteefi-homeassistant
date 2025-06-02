import logging
import os
import asyncio

from homeassistant.components.light import LightEntity, ColorMode, ATTR_BRIGHTNESS, ATTR_RGB_COLOR
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN, ARCH, OS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi lights from a config entry."""
    
    # Get correct integration path
    INTEGRATION_PATH = hass.config.path(f"custom_components/smarteefi")
    
    # Full path to HACLI binary
    if(OS=='win'):
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
    else:
        HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")
    
    _LOGGER.debug(f"Using HACLI path: {HACLI}")

    devices = entry.data.get("devices", [])
    ip_address = entry.data.get("ip_address")
    netmask = entry.data.get("netmask")

    if not devices:
        _LOGGER.error("No devices found for Smarteefi light.")
        return
    
    if not ip_address or not netmask:
        _LOGGER.error("Missing network configuration for Smarteefi light.")
        return

    lights = [SmarteefiLight(device, HACLI, ip_address, netmask) for device in devices if device["type"] == "light"]
    async_add_entities(lights, True)

class SmarteefiLight(LightEntity):
    """Representation of a Smarteefi Light."""

    def __init__(self, device, hacli_path, ip_address, netmask):
        self._device = device
        self._state = False
        self._brightness = 255  # Full brightness by default
        self._rgb_color = (255, 255, 255)  # Default white color
        self._name = device.get("name", "Unnamed Light")
        self._unique_id = device.get("id", "")
        self._cloud_id = device.get("cloudid", "")
        self._hacli = hacli_path
        self.ip_address = ip_address
        self.netmask = netmask
        self._update_unsub = None
        self._smap = None  # Store smap value from entity ID

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
        """Update state from UDP message."""
        received_smap = data["smap"]
        status = data["status"]
        
        # Only process if smap matches our entity's smap
        if received_smap != self._smap:
            return
        
        if status:
            r = (status & 0xFF000000)>>24
            g = (status & 0x00FF0000)>>16
            b = (status & 0x0000FF00)>>8
            self._brightness = int((max(r, g, b) / 255) * 255)  # Scale to 0-255
            self._rgb_color = (r,g,b)
            self._state = True
        else:
            self._state = False 
            self._brightness = 0
            self._rgb_color = (0, 0, 0)

        self.schedule_update_ha_state() 
        _LOGGER.debug(
            f"Updated light {self._name} - "
            f"State: {'on' if self._state else 'off'}, "
            f"Brightness: {self._brightness}, Color: {self._rgb_color}"
        )             
            
        # Your logic: ON if smap == status, OFF if status == 0
        new_state = (status != 0) and (received_smap & status)
        
        if self._state != new_state:
            self._state = new_state
            self.schedule_update_ha_state()
            _LOGGER.debug(
                f"Updated switch {self._name} via UDP - "
                f"State: {'on' if new_state else 'off'}, "
                f"Smap: {received_smap}, Status: {status}"
            )

    @property
    def name(self):
        """Return the name of the light."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID for the light."""
        return self._unique_id

    @property
    def is_on(self):
        """Return True if the light is on."""
        return self._state

    @property
    def brightness(self):
        """Return the brightness of the light."""
        return self._brightness

    @property
    def rgb_color(self):
        """Return the RGB color of the light."""
        return self._rgb_color

    @property
    def supported_color_modes(self):
        """Return supported color modes."""
        return {ColorMode.RGB}

    @property
    def color_mode(self):
        """Return the current color mode."""
        return ColorMode.RGB

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
        """Turn the light on with optional brightness and color control."""
        _LOGGER.info(f"Turning on Smarteefi light: {self._name}")
        
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness)
        rgb_color = kwargs.get(ATTR_RGB_COLOR, self._rgb_color)
        r, g, b = rgb_color

        if r or b or g:
            if await self._execute_cli([self.ip_address, self.netmask, "set-rgb-color", self._unique_id, str(self._cloud_id), self.rgb_to_hex(rgb_color)]):
                self._state = True
                self._rgb_color = rgb_color
                self.schedule_update_ha_state()
        else:
            if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]):
                self._state = False
                self.schedule_update_ha_state()
        
        intensity = self.convert_to_100_range(brightness)
        if intensity:
            if await self._execute_cli([self.ip_address, self.netmask, "set-intensity", self._unique_id, str(self._cloud_id), str(intensity)]):
                self._brightness = brightness
                self.schedule_update_ha_state()   

    async def async_turn_off(self, **kwargs):
        """Turn the light off."""
        _LOGGER.info(f"Turning off Smarteefi light: {self._name}")
        if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]):
            self._state = False
            self.schedule_update_ha_state()

    async def async_set_rgb_color(self, rgb_color):
        """Set the light's RGB color."""
        r, g, b = rgb_color
        _LOGGER.info(f"Setting Smarteefi light {self._name} color to RGB({r}, {g}, {b})")
        if await self._execute_cli([self.ip_address, self.netmask, "set-rgb-color", self._unique_id, str(self._cloud_id), self.rgb_to_hex(rgb_color)]):
            self._rgb_color = rgb_color
            self.schedule_update_ha_state()

    async def async_set_brightness(self, brightness):
        """Set the brightness level."""
        _LOGGER.info(f"Setting Smarteefi light {self._name} brightness to {brightness}")
        intensity = self.convert_to_100_range(brightness)
        if intensity:
            if await self._execute_cli([self.ip_address, self.netmask, "set-intensity", self._unique_id, str(self._cloud_id), str(intensity)]):
                self._brightness = brightness
                self.schedule_update_ha_state()  
        else:
            if await self._execute_cli([self.ip_address, self.netmask, "set-status", self._unique_id, str(self._cloud_id), "0"]):
                self._state = False
                self.schedule_update_ha_state()

    def rgb_to_hex(self, rgb_color):
        r, g, b = [max(0, min(255, x)) for x in rgb_color]
        hex_color = "#{:02X}{:02X}{:02X}".format(r, g, b)
        return hex_color

    def convert_to_100_range(self, value):
        if not 0 <= value <= 255:
            raise ValueError("Value must be between 0 and 255.")
    
        return round((value / 255) * 100)
    
    def rgb_to_brightness(self, r, g, b):
        MAX_DUTY = 255

        if r == 0 and g == 0 and b == 0:
            return 0

        if r == g == b:
            return (100 * r / MAX_DUTY)
        elif r == 0 and g == b:
            return (100 * g / MAX_DUTY)
        elif g == 0 and r == b:
            return (100 * b / MAX_DUTY)
        elif b == 0 and r == g:
            return (100 * r / MAX_DUTY)
        elif r == 0 and g == 0 and b != 0:
            return (100 * b / MAX_DUTY)
        elif g == 0 and b == 0 and r != 0:
            return (100 * r / MAX_DUTY)
        elif r == 0 and b == 0 and g != 0:
            return (100 * g / MAX_DUTY)
        else:
            return (100 * max(r, g, b) / MAX_DUTY)



