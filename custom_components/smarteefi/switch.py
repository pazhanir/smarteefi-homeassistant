"""Smarteefi switch platform — CoordinatorEntity + pure UDP control."""

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import udp_protocol

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Smarteefi switches from a config entry."""
    coordinator = hass.data[DOMAIN]["coordinator"]
    devices = entry.data.get("devices", [])

    switches = [
        SmarteefiSwitch(coordinator, device)
        for device in devices
        if device["type"] == "switch"
    ]

    if switches:
        async_add_entities(switches)
        _LOGGER.debug("Added %d Smarteefi switch entities", len(switches))


class SmarteefiSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a Smarteefi switch channel."""

    def __init__(self, coordinator, device):
        """Initialize the switch."""
        super().__init__(coordinator)
        self._device = device
        self._name = device.get("name", "Unnamed Switch")
        self._unique_id = device["id"]

        # Extract serial and smap from device ID (format: "serial:group_id:smap")
        parts = self._unique_id.split(":")
        self._serial = parts[0]
        self._smap = int(parts[2])

    @property
    def name(self):
        """Return the name of the switch."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the switch."""
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
    def is_on(self):
        """Return True if the switch channel is on."""
        if not self.coordinator.data:
            return False
        module = self.coordinator.data.get(self._serial, {})
        statusmap = module.get("statusmap", 0)
        return (statusmap & self._smap) != 0

    async def async_turn_on(self, **kwargs):
        """Turn the switch on via UDP."""
        _LOGGER.info(
            "Turning ON switch %s (serial=%s, smap=%d)",
            self._name, self._serial, self._smap,
        )
        resp = await udp_protocol.async_set_status(
            self._serial, self.coordinator.broadcast_addr, self._smap, True
        )
        if resp and resp.get("result") == 1:
            self._update_coordinator_from_response(resp)
        else:
            _LOGGER.warning("set-status ON failed for %s, scheduling refresh", self._name)
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off via UDP."""
        _LOGGER.info(
            "Turning OFF switch %s (serial=%s, smap=%d)",
            self._name, self._serial, self._smap,
        )
        resp = await udp_protocol.async_set_status(
            self._serial, self.coordinator.broadcast_addr, self._smap, False
        )
        if resp and resp.get("result") == 1:
            self._update_coordinator_from_response(resp)
        else:
            _LOGGER.warning("set-status OFF failed for %s, scheduling refresh", self._name)
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
