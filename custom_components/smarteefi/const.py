import platform
import zlib

architecture = platform.machine().lower()  # Convert to lowercase for case-insensitive comparison

if architecture in ("x86_64", "amd64"):
    arch = "x86-64bit"
elif architecture in ("i386", "i686", "x86"):
    arch = "x86-32bit"
elif architecture in ("aarch64", "arm64"):
    arch = "arm-64bit"
elif "armv" in architecture:  # ARM 32-bit (e.g., armv7l, armv6l)
    arch = "arm-32bit"
else:
    arch = "unknown"  # Default for unsupported or unknown architectures

system = platform.system()
if system == 'Windows':
    os = 'win'
elif system == 'Linux':
    os = 'linux'
elif system == 'Darwin':
    os = 'mac'
else:
    os = 'unknown'


DOMAIN = 'smarteefi'
ARCH = arch
OS=os

INITIAL_SYNC_INTERVAL = 5 # Sync interval for the first sync after HA Restart
SYNC_INTERVAL = 15 # Sync interval in seconds

API_BASE_URL = "https://www.smarteefi.com/api/v3"
API_LOGIN_URL = API_BASE_URL + "/user/login"
API_DEVICES_URL = API_BASE_URL + "/user/devices"

# Far-future expiry timestamp (Jan 1, 2099 UTC) for cloudid generation.
# The CLI binary decrypts cloudid as: strtoul(cloudid) XOR crc32(device_id)
# and checks the result is a future Unix timestamp above 0x6774857f.
_CLOUDID_EXPIRY_TIMESTAMP = 4102444800

def generate_cloudid(device_id: str) -> str:
    """Generate a valid cloudid for a device.

    The CLI binary's subscription check decrypts cloudid by XORing it with
    CRC32 of the device_id string. The result must be a Unix timestamp in the
    future. We use a far-future timestamp (year 2099) to avoid expiry.

    Args:
        device_id: The device ID string (e.g. "ABC123:1:2").

    Returns:
        The cloudid as a decimal string suitable for passing to the CLI.
    """
    crc = zlib.crc32(device_id.encode()) & 0xFFFFFFFF
    cloudid = _CLOUDID_EXPIRY_TIMESTAMP ^ crc
    return str(cloudid)