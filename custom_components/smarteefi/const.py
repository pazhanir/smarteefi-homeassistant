import platform

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