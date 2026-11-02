"""Coralboard peripheral-impersonation bench (milestone 1).

Stdlib-only. Nothing here talks to hardware by itself; the serial controller
requires an explicit device path and the USB controller an exact USB serial
number, each with a deliberate command.
"""

__all__ = [
    "profile",
    "simulator",
    "evidence",
    "replay",
    "serial_link",
    "usb_link",
    "controller",
]

__version__ = "0.1.0"
MILESTONE = "m1-i2c-register-map-sim"
