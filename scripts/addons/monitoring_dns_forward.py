# SPDX-License-Identifier: MIT
"""Manage split-DNS forwarding required by an internal monitoring OIDC issuer."""
import argparse
import ipaddress
import json
import re
import secrets
import socket
import struct
import sys
from pathlib import Path


def validate_zone(value):
    if not isinstance(value, str) or value.endswith(".") or "." not in value:
        raise ValueError("invalid DNS zone")
    labels = value.split(".")
    if any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?", item)
           or len(item) > 63 for item in labels):
        raise ValueError("invalid DNS zone")
    return value.lower()


def validate_resolver(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("internal DNS resolver must be an IP address") from None
    if (address.version != 4 or not address.is_private or address.is_loopback
            or address.is_link_local or address.is_unspecified or address.is_multicast):
        raise ValueError("internal DNS resolver must be a private IPv4 address")
    return str(address)


def ensure_forward(corefile, zone, resolver):
    """Add a split-zone forward before the root server block; refuse conflicts."""
    zone, resolver = validate_zone(zone), validate_resolver(resolver)
    if not isinstance(corefile, str) or not corefile.strip():
        raise ValueError("CoreDNS Corefile is empty")
    zone_header = re.compile(r"(?m)^\s*" + re.escape(zone) + r"(?::53)?\s*\{")
    match = zone_header.search(corefile)
    if match:
        start = match.start()
        end = corefile.find("\n}", match.end())
        if end < 0:
            raise ValueError("existing CoreDNS zone block is incomplete")
        block = corefile[start:end + 2]
        if re.search(r"(?m)^\s*forward\s+\.\s+" + re.escape(resolver) + r"(?:\s|$)", block):
            return corefile
        raise ValueError("the internal DNS zone already has a different CoreDNS configuration")

    root = re.search(r"(?m)^\s*\.(:53)?\s*\{", corefile)
    if not root:
        raise ValueError("CoreDNS root server block was not found")
    block = (f"{zone}:53 {{\n"
             f"    errors\n"
             f"    cache 30\n"
             f"    forward . {resolver}\n"
             f"}}\n\n")
    return corefile[:root.start()] + block + corefile[root.start():]


def _skip_name(packet, offset):
    while offset < len(packet):
        length = packet[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise ValueError("truncated DNS compression pointer")
            return offset + 2
        offset += 1
        if length == 0:
            return offset
        offset += length
    raise ValueError("truncated DNS name")


def parse_ipv4_answers(packet, transaction_id):
    if len(packet) < 12:
        raise ValueError("truncated DNS response")
    response_id, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if response_id != transaction_id or not flags & 0x8000 or flags & 0x000F:
        raise ValueError("DNS response was invalid or returned an error")
    offset = 12
    for _ in range(questions):
        offset = _skip_name(packet, offset) + 4
    resolved = set()
    for _ in range(answers):
        offset = _skip_name(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("truncated DNS answer")
        record_type, record_class, _, size = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        if offset + size > len(packet):
            raise ValueError("truncated DNS answer data")
        if record_type == 1 and record_class == 1 and size == 4:
            resolved.add(socket.inet_ntoa(packet[offset:offset + 4]))
        offset += size
    return sorted(resolved, key=lambda item: tuple(int(part) for part in item.split(".")))


def _read_exact(connection, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise RuntimeError("truncated TCP DNS response")
        chunks.extend(chunk)
    return bytes(chunks)


def lookup_ipv4(server, name, timeout=3, port=53, tcp=False):
    if tcp and server == "127.0.0.1":
        server = "127.0.0.1"
    else:
        server = validate_resolver(server)
    if not isinstance(name, str) or not name or len(name) > 253:
        raise ValueError("invalid DNS query name")
    transaction_id = secrets.randbits(16)
    labels = name.rstrip(".").split(".")
    try:
        encoded_name = b"".join(bytes([len(label)]) + label.encode("idna") for label in labels) + b"\0"
    except (UnicodeError, ValueError):
        raise ValueError("invalid DNS query name") from None
    packet = struct.pack("!HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0) + encoded_name + struct.pack("!HH", 1, 1)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid DNS port")
    try:
        if tcp:
            with socket.create_connection((server, port), timeout=timeout) as client:
                client.settimeout(timeout)
                client.sendall(struct.pack("!H", len(packet)) + packet)
                response_length = struct.unpack("!H", _read_exact(client, 2))[0]
                if response_length < 12 or response_length > 4096:
                    raise RuntimeError("invalid TCP DNS response length")
                response = _read_exact(client, response_length)
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.settimeout(timeout)
                client.sendto(packet, (server, port))
                response, _ = client.recvfrom(4096)
    except OSError:
        raise RuntimeError("internal DNS query failed") from None
    answers = parse_ipv4_answers(response, transaction_id)
    if not answers:
        raise RuntimeError("internal DNS query returned no IPv4 address")
    return answers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--zone")
    parser.add_argument("--resolver")
    parser.add_argument("--corefile")
    parser.add_argument("--server")
    parser.add_argument("--name")
    parser.add_argument("--expected-address")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--tcp", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.render:
            if not args.zone or not args.resolver or not args.corefile:
                parser.error("--render requires --zone, --resolver, and --corefile")
            print(ensure_forward(Path(args.corefile).read_text(), args.zone, args.resolver), end="")
            return 0
        if args.probe:
            if not args.server or not args.name:
                parser.error("--probe requires --server and --name")
            display_server = ("127.0.0.1" if args.tcp and args.server == "127.0.0.1"
                              else validate_resolver(args.server))
            answers = lookup_ipv4(args.server, args.name, port=args.port, tcp=args.tcp)
            if args.expected_address:
                expected = str(ipaddress.IPv4Address(args.expected_address))
                if expected not in answers:
                    raise RuntimeError("DNS response did not contain the expected IPv4 address")
            print(json.dumps({"server": display_server,
                              "name": args.name, "answers": answers}, separators=(",", ":")))
            return 0
        parser.error("--render or --probe is required")
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
