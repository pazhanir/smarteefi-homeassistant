import logging
import aiohttp
import os
import stat
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from .const import DOMAIN, ARCH, OS

_LOGGER = logging.getLogger(__name__)

API_URL = "https://www.smarteefi.com/api/homeassistant_v1/user/validatehatoken"

class SmarteefiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Smarteefi IoT Platform."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Prompt user to enter apitoken."""
        errors = {}  # Reinitialize errors dictionary

        # Get correct integration path
        INTEGRATION_PATH = self.hass.config.path(f"custom_components/smarteefi")
    
        # Full path to HACLI binary
        if(OS=='win'):
            HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli.exe")
        else:
            HACLI = os.path.join(INTEGRATION_PATH, f"hacli-{OS}-{ARCH}", f"smarteefi-ha-cli")
    
        self.set_executable_permissions(HACLI)
        _LOGGER.debug(f"Using HACLI path: {HACLI}")            

        if user_input is not None:
            apitoken = user_input["apitoken"]

            # Validate the API token
            validation_result = await self._validate_api_token(apitoken)
            if validation_result.get("result") != "success":
                _LOGGER.error(f"Validation Result: {validation_result}")
                myerror = validation_result.get("error_desc", "invalid_token")
                _LOGGER.error(f"API token validation failed myerror: {myerror}")
                errors["base"] = validation_result.get("error_desc", "invalid_token")  # Update errors with new error
                _LOGGER.error(f"API token validation failed: {errors['base']}")
            else:
                # Store apitoken and set network_interface, ip_address, and netmask to empty strings
                return self.async_create_entry(
                    title=DOMAIN, 
                    data={
                        "apitoken": apitoken,
                        "network_interface": "",
                        "ip_address": "",
                        "netmask": "",
                    }
                )

        # Show form for entering apitoken
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("apitoken"): str,
            }),
            errors=errors,  # Pass the updated errors dictionary
        )
    
    def set_executable_permissions(self, file_path: str) -> None:
        """Set executable permissions on the specified file."""
        if os.path.exists(file_path):
            # Get current permissions
            current_perms = os.stat(file_path).st_mode
            # Add execute permission for owner, group, and others (equivalent to chmod +x)
            os.chmod(file_path, current_perms | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            # Log an error if the file isn’t found (optional, requires hass.logger)
            _LOGGER(f"CLI not found at {file_path}")

    async def _validate_api_token(self, apitoken):
        """Validate the API token by making a POST request to the cloud API."""
        payload = {
            "UserDevice": {
                "hatoken": apitoken
            }
        }
        headers = {
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL, json=payload, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data
                    else:
                        _LOGGER.error(f"API request failed with status code: {response.status}")
                        return {"result": "error", "error_desc": "API request failed"}
        except Exception as e:
            _LOGGER.error(f"Exception occurred while validating API token: {e}")
            return {"result": "error", "error_desc": str(e)}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return SmarteefiOptionsFlowHandler(config_entry)


class SmarteefiOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for editing apitoken."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Allow user to update apitoken."""
        errors = {}  # Reinitialize errors dictionary

        if user_input is not None:
            # Validate the API token
            validation_result = await self._validate_api_token(user_input["apitoken"])
            if validation_result.get("result") != "success":
                errors["base"] = validation_result.get("error_desc", "invalid_token")  # Update errors with new error
            else:
                # Update config entry with new data
                return self.async_create_entry(
                    title="",
                    data={
                        "apitoken": user_input["apitoken"],
                        "network_interface": self.config_entry.data["network_interface"],
                        "ip_address": self.config_entry.data["ip_address"],
                        "netmask": self.config_entry.data["netmask"],
                    },
                )

        # Show form for updating apitoken
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("apitoken", default=self.config_entry.data["apitoken"]): str,
            }),
            errors=errors,  # Pass the updated errors dictionary
        )

    async def _validate_api_token(self, apitoken):
        """Validate the API token by making a POST request to the cloud API."""
        payload = {
            "UserDevice": {
                "hatoken": apitoken
            }
        }
        headers = {
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(API_URL, json=payload, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data
                    else:
                        _LOGGER.error(f"API request failed with status code: {response.status}")
                        return {"result": "error", "error_desc": "API request failed"}
        except Exception as e:
            _LOGGER.error(f"Exception occurred while validating API token: {e}")
            return {"result": "error", "error_desc": str(e)}
        
