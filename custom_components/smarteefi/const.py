"""Constants for the Smarteefi integration."""

DOMAIN = 'smarteefi'

INITIAL_SYNC_INTERVAL = 5   # Sync interval for the first sync after HA restart
SYNC_INTERVAL = 5            # Regular sync interval in seconds (push updates unreliable, polling is primary)

API_BASE_URL = "https://www.smarteefi.com/api/v3"
API_LOGIN_URL = API_BASE_URL + "/user/login"
API_DEVICES_URL = API_BASE_URL + "/user/devices"
