import logging
import os
import asyncio

from homeassistant.components.light import LightEntity, ColorMode, ATTR_BRIGHTNESS, ATTR_RGB_COLOR
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

