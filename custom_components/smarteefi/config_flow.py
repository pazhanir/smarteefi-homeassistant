import logging
import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, API_LOGIN_URL, API_DEVICES_URL

_LOGGER = logging.getLogger(__name__)

DEVICE_TYPES = ["switch", "fan", "light", "cover"]


class SmarteefiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Smarteefi IoT Platform."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self._data = {}
        self._devices_raw = []

    async def async_step_user(self, user_input=None):
        """Step 1: Prompt user for email and password, login via v3 API."""
        errors = {}

        if user_input is not None:
            email = user_input["email"]
            password = user_input["password"]

            result = await self._api_login(email, password)

            if result.get("result") == "success":
                self._data["email"] = email
                self._data["password"] = password
                self._data["access_token"] = result["access_token"]
                return await self.async_step_devices()
            else:
                error_desc = result.get("error_desc", "invalid_credentials")
                _LOGGER.error("Smarteefi login failed: %s", error_desc)
                errors["base"] = "invalid_credentials"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("email"): str,
                vol.Required("password"): str,
            }),
            errors=errors,
        )

    async def async_step_devices(self, user_input=None):
        """Step 2: Fetch devices and let user select type for each."""
        errors = {}

        if user_input is not None:
            # Build device list from user selections
            devices = []
            for key, device_type in user_input.items():
                # Key format: type_SERIAL_GROUPID_MAP
                parts = key.split("_", 1)  # Split off "type_" prefix
                if len(parts) != 2:
                    continue
                device_key = parts[1]  # SERIAL_GROUPID_MAP
                # Find matching raw device
                for raw in self._devices_raw:
                    raw_key = f"{raw['serial']}_{raw['group_id']}_{raw['map']}"
                    if raw_key == device_key:
                        device_id = f"{raw['serial']}:{raw['group_id']}:{int(raw['map'])}"
                        devices.append({
                            "id": device_id,
                            "type": device_type,
                            "name": raw["name"],
                        })
                        break

            return self.async_create_entry(
                title=DOMAIN,
                data={
                    "email": self._data["email"],
                    "password": self._data["password"],
                    "access_token": self._data["access_token"],
                    "network_interface": "",
                    "ip_address": "",
                    "netmask": "",
                    "devices": devices,
                },
            )

        # Fetch devices from v3 API
        result = await self._api_fetch_devices(self._data["access_token"])

        if result.get("result") != "success":
            _LOGGER.error("Failed to fetch devices: %s", result)
            errors["base"] = "api_error"
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }),
                errors=errors,
            )

        switches = result.get("switches", [])
        if not switches:
            _LOGGER.warning("No devices found on Smarteefi account")
            errors["base"] = "no_devices"
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }),
                errors=errors,
            )

        self._devices_raw = switches

        # Build dynamic schema with one dropdown per device
        schema_dict = {}
        for sw in switches:
            key = f"type_{sw['serial']}_{sw['group_id']}_{sw['map']}"
            label = sw.get("name", sw["serial"])
            schema_dict[vol.Required(key, default="switch", description={"suggested_value": "switch"})] = vol.In(
                {t: t.capitalize() for t in DEVICE_TYPES}
            )

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={
                "device_count": str(len(switches)),
            },
        )

    async def _api_login(self, email, password):
        """Login to Smarteefi v3 API."""
        payload = {
            "LoginForm": {
                "email": email,
                "password": password,
                "app": "smarteefi",
            }
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_LOGIN_URL, json=payload, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        _LOGGER.error("Login API returned status %s", response.status)
                        return {"result": "error", "error_desc": "api_error"}
        except Exception as e:
            _LOGGER.error("Exception during login: %s", e)
            return {"result": "error", "error_desc": str(e)}

    async def _api_fetch_devices(self, access_token):
        """Fetch devices from Smarteefi v3 API."""
        payload = {
            "UserDevice": {
                "access_token": access_token,
            }
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_DEVICES_URL, json=payload, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        _LOGGER.error("Devices API returned status %s", response.status)
                        return {"result": "error", "error_desc": "api_error"}
        except Exception as e:
            _LOGGER.error("Exception during device fetch: %s", e)
            return {"result": "error", "error_desc": str(e)}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return SmarteefiOptionsFlowHandler(config_entry)


class SmarteefiOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for re-entering credentials and re-selecting device types."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry
        self._data = {}
        self._devices_raw = []

    async def async_step_init(self, user_input=None):
        """Step 1: Re-enter email and password."""
        errors = {}

        if user_input is not None:
            email = user_input["email"]
            password = user_input["password"]

            result = await self._api_login(email, password)

            if result.get("result") == "success":
                self._data["email"] = email
                self._data["password"] = password
                self._data["access_token"] = result["access_token"]
                return await self.async_step_devices()
            else:
                error_desc = result.get("error_desc", "invalid_credentials")
                _LOGGER.error("Smarteefi login failed during options: %s", error_desc)
                errors["base"] = "invalid_credentials"

        current_email = self.config_entry.data.get("email", "")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("email", default=current_email): str,
                vol.Required("password"): str,
            }),
            errors=errors,
        )

    async def async_step_devices(self, user_input=None):
        """Step 2: Re-fetch devices and let user re-select types."""
        errors = {}

        if user_input is not None:
            # Build device list from user selections
            devices = []
            for key, device_type in user_input.items():
                parts = key.split("_", 1)
                if len(parts) != 2:
                    continue
                device_key = parts[1]
                for raw in self._devices_raw:
                    raw_key = f"{raw['serial']}_{raw['group_id']}_{raw['map']}"
                    if raw_key == device_key:
                        device_id = f"{raw['serial']}:{raw['group_id']}:{int(raw['map'])}"
                        devices.append({
                            "id": device_id,
                            "type": device_type,
                            "name": raw["name"],
                        })
                        break

            # Update config entry with new credentials and devices
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    "email": self._data["email"],
                    "password": self._data["password"],
                    "access_token": self._data["access_token"],
                    "devices": devices,
                },
            )

            return self.async_create_entry(title="", data={})

        # Fetch devices from v3 API
        result = await self._api_fetch_devices(self._data["access_token"])

        if result.get("result") != "success":
            _LOGGER.error("Failed to fetch devices during options: %s", result)
            errors["base"] = "api_error"
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }),
                errors=errors,
            )

        switches = result.get("switches", [])
        if not switches:
            _LOGGER.warning("No devices found during options flow")
            errors["base"] = "no_devices"
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({
                    vol.Required("email"): str,
                    vol.Required("password"): str,
                }),
                errors=errors,
            )

        self._devices_raw = switches

        # Build existing type map from current config for defaults
        current_devices = self.config_entry.data.get("devices", [])
        current_type_map = {d["id"]: d["type"] for d in current_devices}

        schema_dict = {}
        for sw in switches:
            key = f"type_{sw['serial']}_{sw['group_id']}_{sw['map']}"
            device_id = f"{sw['serial']}:{sw['group_id']}:{int(sw['map'])}"
            default_type = current_type_map.get(device_id, "switch")
            schema_dict[vol.Required(key, default=default_type)] = vol.In(
                {t: t.capitalize() for t in DEVICE_TYPES}
            )

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def _api_login(self, email, password):
        """Login to Smarteefi v3 API."""
        payload = {
            "LoginForm": {
                "email": email,
                "password": password,
                "app": "smarteefi",
            }
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_LOGIN_URL, json=payload, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        _LOGGER.error("Login API returned status %s", response.status)
                        return {"result": "error", "error_desc": "api_error"}
        except Exception as e:
            _LOGGER.error("Exception during login: %s", e)
            return {"result": "error", "error_desc": str(e)}

    async def _api_fetch_devices(self, access_token):
        """Fetch devices from Smarteefi v3 API."""
        payload = {
            "UserDevice": {
                "access_token": access_token,
            }
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_DEVICES_URL, json=payload, headers=headers) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        _LOGGER.error("Devices API returned status %s", response.status)
                        return {"result": "error", "error_desc": "api_error"}
        except Exception as e:
            _LOGGER.error("Exception during device fetch: %s", e)
            return {"result": "error", "error_desc": str(e)}
