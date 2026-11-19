# SPDX-License-Identifier: MIT
"""Minimal systemd service-manager notification transport."""
import os
import socket


def notify(message):
    """Send a notify datagram, returning False outside a notify service."""
    if not isinstance(message, str) or not message or "\x00" in message:
        raise ValueError("invalid systemd notification")
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\x00" + address[1:]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.connect(address)
        client.sendall(message.encode("utf-8"))
    finally:
        client.close()
    return True
