"""Smarteefi light platform — CoordinatorEntity + pure UDP control."""

import logging

from homeassistant.components.light import LightEntity, ColorMode, ATTR_BRIGHTNESS, ATTR_RGB_COLOR
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import udp_protocol

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi lights from a config entry."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    devices = entry.data.get("devices", [])

    lights = [
        SmarteefiLight(coordinator, device)
        for device in devices
        if device["type"] == "light"
    ]

    if lights:
        async_add_entities(lights)
        _LOGGER.debug("Added %d Smarteefi light entities", len(lights))


class SmarteefiLight(CoordinatorEntity, LightEntity):
    """Representation of a Smarteefi light channel."""

    def __init__(self, coordinator, device):
        """Initialize the light."""
        super().__init__(coordinator)
        self._device = device
        self._name = device.get("name", "Unnamed Light")
        self._unique_id = device["id"]

        # Extract serial and smap from device ID (format: "serial:group_id:smap")
        parts = self._unique_id.split(":")
        self._serial = parts[0]
        self._smap = int(parts[2])

        # Local state for brightness/color (not always derivable from statusmap alone)
        self._brightness = 255
        self._rgb_color = (255, 255, 255)

    @property
    def name(self):
        """Return the name of the light."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the light."""
        return self._unique_id

    @property
    def available(self):
        """Return True if the device module is available."""
        if not self.coordinator.data:
            return False
        module = self.coordinator.data.get(self._serial)
        if module is None:
            return False
        return module.get("available", False)

    @property
    def supported_color_modes(self):
        """Return supported color modes."""
        return {ColorMode.RGB}

    @property
    def color_mode(self):
        """Return the current color mode."""
        return ColorMode.RGB

    @property
    def is_on(self):
        """Return True if the light channel is on."""
        if not self.coordinator.data:
            return False
        module = self.coordinator.data.get(self._serial, {})
        statusmap = module.get("statusmap", 0)
        return (statusmap & self._smap) != 0

    @property
    def brightness(self):
        """Return the brightness of the light."""
        if not self.coordinator.data:
            return 0
        module = self.coordinator.data.get(self._serial, {})
        statusmap = module.get("statusmap", 0)

        if statusmap == 0:
            return 0

        # Extract RGB from statusmap and derive brightness
        r = (statusmap & 0xFF000000) >> 24
        g = (statusmap & 0x00FF0000) >> 16
        b = (statusmap & 0x0000FF00) >> 8
        brightness = max(r, g, b)
        if brightness > 0:
            self._brightness = brightness
        return self._brightness

    @property
    def rgb_color(self):
        """Return the RGB color of the light."""
        if not self.coordinator.data:
            return (0, 0, 0)
        module = self.coordinator.data.get(self._serial, {})
        statusmap = module.get("statusmap", 0)

        if statusmap == 0:
            return (0, 0, 0)

        r = (statusmap & 0xFF000000) >> 24
        g = (statusmap & 0x00FF0000) >> 16
        b = (statusmap & 0x0000FF00) >> 8
        if r or g or b:
            self._rgb_color = (r, g, b)
        return self._rgb_color

    async def async_turn_on(self, **kwargs):
        """Turn the light on with optional brightness and color control."""
        _LOGGER.info("Turning ON light %s (serial=%s, smap=%d)", self._name, self._serial, self._smap)

        brightness = kwargs.get(ATTR_BRIGHTNESS, self._brightness)
        rgb_color = kwargs.get(ATTR_RGB_COLOR, self._rgb_color)
        r, g, b = rgb_color

        if r or g or b:
            # Set RGB color
            resp = await udp_protocol.async_set_rgb_color(
                self._serial, self.coordinator.broadcast_addr, self._smap, r, g, b
            )
            if resp and resp.get("result") == 1:
                self._rgb_color = rgb_color
                self._update_coordinator_from_response(resp)
            else:
                _LOGGER.warning("set-rgb-color failed for light %s", self._name)
                await self.coordinator.async_request_refresh()
        else:
            # RGB is (0,0,0) — turn off instead
            resp = await udp_protocol.async_set_status(
                self._serial, self.coordinator.broadcast_addr, self._smap, False
            )
            if resp and resp.get("result") == 1:
                self._update_coordinator_from_response(resp)
            else:
                await self.coordinator.async_request_refresh()
            return

        # Set intensity (brightness 0-255 → 0-100)
        intensity = self._brightness_to_intensity(brightness)
        if intensity:
            resp = await udp_protocol.async_set_intensity(
                self._serial, self.coordinator.broadcast_addr, self._smap, intensity
            )
            if resp and resp.get("result") == 1:
                self._brightness = brightness
                self._update_coordinator_from_response(resp)
            else:
                _LOGGER.warning("set-intensity failed for light %s", self._name)
                await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off via UDP."""
        _LOGGER.info("Turning OFF light %s (serial=%s, smap=%d)", self._name, self._serial, self._smap)
        resp = await udp_protocol.async_set_status(
            self._serial, self.coordinator.broadcast_addr, self._smap, False
        )
        if resp and resp.get("result") == 1:
            self._update_coordinator_from_response(resp)
        else:
            _LOGGER.warning("set-status OFF failed for light %s", self._name)
            await self.coordinator.async_request_refresh()

    def _update_coordinator_from_response(self, resp):
        """Merge a UDP command response into coordinator data."""
        new_data = dict(self.coordinator.data) if self.coordinator.data else {}
        module = dict(new_data.get(self._serial, {}))
        module["statusmap"] = resp.get("statusmap", module.get("statusmap", 0))
        module["switchmap"] = resp.get("switchmap", module.get("switchmap", 0))
        module["available"] = True
        new_data[self._serial] = module
        self.coordinator.async_set_updated_data(new_data)

    @staticmethod
    def _brightness_to_intensity(brightness: int) -> int:
        """Convert HA brightness (0-255) to Smarteefi intensity (0-100)."""
        if not 0 <= brightness <= 255:
            brightness = max(0, min(255, brightness))
        return round((brightness / 255) * 100)
