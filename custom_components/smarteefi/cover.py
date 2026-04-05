"""Smarteefi cover platform — CoordinatorEntity + pure UDP control."""

import logging

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import udp_protocol

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi covers from a config entry."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    devices = entry.data.get("devices", [])

    covers = [
        SmarteefiCover(coordinator, device)
        for device in devices
        if device["type"] == "cover"
    ]

    if covers:
        async_add_entities(covers)
        _LOGGER.debug("Added %d Smarteefi cover entities", len(covers))


class SmarteefiCover(CoordinatorEntity, CoverEntity):
    """Representation of a Smarteefi cover channel."""

    def __init__(self, coordinator, device):
        """Initialize the cover."""
        super().__init__(coordinator)
        self._device = device
        self._name = device.get("name", "Unnamed Cover")
        self._unique_id = device["id"]

        # Extract serial and smap from device ID (format: "serial:group_id:smap")
        parts = self._unique_id.split(":")
        self._serial = parts[0]
        self._smap = int(parts[2])

    @property
    def name(self):
        """Return the name of the cover."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the cover."""
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
    def supported_features(self):
        """Return supported features."""
        return CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.SET_POSITION

    @property
    def is_closed(self):
        """Return True if the cover is closed."""
        if not self.coordinator.data:
            return True  # Assume closed if no data
        module = self.coordinator.data.get(self._serial, {})
        statusmap = module.get("statusmap", 0)
        return (statusmap & self._smap) == 0

    @property
    def current_cover_position(self):
        """Return the current position of the cover (0=closed, 100=open)."""
        if self.is_closed:
            return 0
        return 100

    async def async_open_cover(self, **kwargs):
        """Open the cover via UDP, serialized per module."""
        lock = self.coordinator.get_serial_lock(self._serial)
        async with lock:
            _LOGGER.info(
                "Opening cover %s (serial=%s, smap=0x%X)",
                self._name, self._serial, self._smap,
            )
            resp = await udp_protocol.async_set_status(
                self._serial, self.coordinator.broadcast_addr, self._smap, True
            )
            if resp and resp.get("result") == 1:
                _LOGGER.debug(
                    "OPEN response for cover %s: switchmap=0x%X, statusmap=0x%X",
                    self._name, resp.get("switchmap", 0), resp.get("statusmap", 0),
                )
                self._update_coordinator_from_response(resp)
            else:
                _LOGGER.warning("set-status OPEN failed for cover %s (resp=%s)", self._name, resp)
                await self.coordinator.async_request_refresh()

    async def async_close_cover(self, **kwargs):
        """Close the cover via UDP, serialized per module."""
        lock = self.coordinator.get_serial_lock(self._serial)
        async with lock:
            _LOGGER.info(
                "Closing cover %s (serial=%s, smap=0x%X)",
                self._name, self._serial, self._smap,
            )
            resp = await udp_protocol.async_set_status(
                self._serial, self.coordinator.broadcast_addr, self._smap, False
            )
            if resp and resp.get("result") == 1:
                _LOGGER.debug(
                    "CLOSE response for cover %s: switchmap=0x%X, statusmap=0x%X",
                    self._name, resp.get("switchmap", 0), resp.get("statusmap", 0),
                )
                self._update_coordinator_from_response(resp)
            else:
                _LOGGER.warning("set-status CLOSE failed for cover %s (resp=%s)", self._name, resp)
                await self.coordinator.async_request_refresh()

    async def async_set_cover_position(self, **kwargs):
        """Set the cover position, serialized per module."""
        position = kwargs.get("position", 0)
        _LOGGER.info("Setting cover %s position to %d", self._name, position)

        if position == 0:
            await self.async_close_cover()
        elif position == 100:
            await self.async_open_cover()
        else:
            # Partial position: determine direction based on current state
            lock = self.coordinator.get_serial_lock(self._serial)
            async with lock:
                current_pos = self.current_cover_position
                turn_on = position > current_pos
                resp = await udp_protocol.async_set_status(
                    self._serial, self.coordinator.broadcast_addr, self._smap, turn_on
                )

                if resp and resp.get("result") == 1:
                    self._update_coordinator_from_response(resp)
                else:
                    _LOGGER.warning("set-position failed for cover %s (resp=%s)", self._name, resp)
                    await self.coordinator.async_request_refresh()

    def _update_coordinator_from_response(self, resp):
        """Merge a UDP command response into coordinator data."""
        new_data = dict(self.coordinator.data) if self.coordinator.data else {}
        module = dict(new_data.get(self._serial, {}))
        module["statusmap"] = resp.get("statusmap", module.get("statusmap", 0))
        module["switchmap"] = resp.get("switchmap", module.get("switchmap", 0))
        module["available"] = True
        new_data[self._serial] = module
        # Mark command time so the next poll doesn't overwrite this fresh data
        self.coordinator.mark_command_time(self._serial)
        self.coordinator.async_set_updated_data(new_data)
