"""Smarteefi fan platform — CoordinatorEntity + pure UDP control."""

import logging
import math

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.util.percentage import ranged_value_to_percentage, percentage_to_ranged_value
from homeassistant.util.scaling import int_states_in_range
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import udp_protocol

_LOGGER = logging.getLogger(__name__)

SPEED_RANGE = (1, 4)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi fans from a config entry."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    devices = entry.data.get("devices", [])

    fans = [
        SmarteefiFan(coordinator, device)
        for device in devices
        if device["type"] == "fan"
    ]

    if fans:
        async_add_entities(fans)
        _LOGGER.debug("Added %d Smarteefi fan entities", len(fans))


class SmarteefiFan(CoordinatorEntity, FanEntity):
    """Representation of a Smarteefi fan channel."""

    def __init__(self, coordinator, device):
        """Initialize the fan."""
        super().__init__(coordinator)
        self._device = device
        self._name = device.get("name", "Unnamed Fan")
        self._unique_id = device["id"]

        # Extract serial and smap from device ID (format: "serial:group_id:smap")
        parts = self._unique_id.split(":")
        self._serial = parts[0]
        self._smap = int(parts[2])

    @property
    def name(self):
        """Return the name of the fan."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the fan."""
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
        return FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF | FanEntityFeature.SET_SPEED

    @property
    def is_on(self) -> bool | None:
        """Return True if the fan channel is on."""
        if not self.coordinator.data:
            return False
        module = self.coordinator.data.get(self._serial, {})
        statusmap = module.get("statusmap", 0)
        return (statusmap & self._smap) != 0

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage."""
        speed = self._extract_speed()
        if speed == 0:
            return 0
        return ranged_value_to_percentage(SPEED_RANGE, speed)

    @property
    def speed_count(self) -> int:
        """Return the number of speeds the fan supports."""
        return int_states_in_range(SPEED_RANGE)

    def _extract_speed(self) -> int:
        """Extract fan speed (0-4) from statusmap bits 4-6."""
        if not self.coordinator.data:
            return 0
        module = self.coordinator.data.get(self._serial, {})
        statusmap = module.get("statusmap", 0)

        if statusmap == 0:
            return 0

        r1 = statusmap & 0x10
        r2 = statusmap & 0x20
        r3 = statusmap & 0x40

        if r3:
            return 4
        elif r2 and r1:
            return 3
        elif r2:
            return 2
        elif r1:
            return 1
        return 0

    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs) -> None:
        """Turn the fan on via UDP, serialized per module."""
        lock = self.coordinator.get_serial_lock(self._serial)
        async with lock:
            _LOGGER.info(
                "Turning ON fan %s (serial=%s, smap=0x%X)",
                self._name, self._serial, self._smap,
            )
            resp = await udp_protocol.async_set_status(
                self._serial, self.coordinator.broadcast_addr, self._smap, True
            )
            if resp and resp.get("result") == 1:
                _LOGGER.debug(
                    "ON response for fan %s: switchmap=0x%X, statusmap=0x%X",
                    self._name, resp.get("switchmap", 0), resp.get("statusmap", 0),
                )
                self._update_coordinator_from_response(resp)
            else:
                _LOGGER.warning("set-status ON failed for fan %s (resp=%s)", self._name, resp)
                await self.coordinator.async_request_refresh()

        if percentage is not None:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the fan off via UDP, serialized per module."""
        lock = self.coordinator.get_serial_lock(self._serial)
        async with lock:
            _LOGGER.info(
                "Turning OFF fan %s (serial=%s, smap=0x%X)",
                self._name, self._serial, self._smap,
            )
            resp = await udp_protocol.async_set_status(
                self._serial, self.coordinator.broadcast_addr, self._smap, False
            )
            if resp and resp.get("result") == 1:
                _LOGGER.debug(
                    "OFF response for fan %s: switchmap=0x%X, statusmap=0x%X",
                    self._name, resp.get("switchmap", 0), resp.get("statusmap", 0),
                )
                self._update_coordinator_from_response(resp)
            else:
                _LOGGER.warning("set-status OFF failed for fan %s (resp=%s)", self._name, resp)
                await self.coordinator.async_request_refresh()

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the fan speed via UDP, serialized per module."""
        speed = math.ceil(percentage_to_ranged_value(SPEED_RANGE, percentage))
        _LOGGER.info("Setting fan %s speed to %d (percentage=%d)", self._name, speed, percentage)

        if speed:
            lock = self.coordinator.get_serial_lock(self._serial)
            async with lock:
                resp = await udp_protocol.async_set_speed(
                    self._serial, self.coordinator.broadcast_addr, self._smap, speed
                )
                if resp and resp.get("result") == 1:
                    self._update_coordinator_from_response(resp)
                else:
                    _LOGGER.warning("set-speed failed for fan %s (resp=%s)", self._name, resp)
                    await self.coordinator.async_request_refresh()
        else:
            # Speed 0 = turn off
            await self.async_turn_off()

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
