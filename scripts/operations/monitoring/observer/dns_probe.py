# SPDX-License-Identifier: MIT
"""Perform an explicit DNS query and compare answers with an expected VIP set."""
import ipaddress
import secrets
import socket
import struct


def validate(resolver, name, expected):
    try:
        resolver = str(ipaddress.ip_address(resolver))
        if not isinstance(name, str) or len(name) > 253 or name.endswith("."):
            raise ValueError()
        labels = name.split(".")
        if any(not label or len(label) > 63 or not label[0].isalnum() or not label[-1].isalnum() or
               any(not (char.isalnum() or char == "-") for char in label) for label in labels):
            raise ValueError()
        name.encode("ascii")
        if not isinstance(expected, (list, tuple)) or not 1 <= len(expected) <= 16:
            raise ValueError()
        addresses = sorted({str(ipaddress.ip_address(item)) for item in expected})
        if not addresses:
            raise ValueError()
        return resolver, name.lower(), tuple(addresses)
    except (TypeError, ValueError):
        raise ValueError("invalid DNS probe target") from None


def _query_packet(name, record_type, transaction):
    header = struct.pack("!HHHHHH", transaction, 0x0100, 1, 0, 0, 0)
    question = b"".join(bytes((len(label),)) + label.encode("ascii") for label in name.split(".")) + b"\0"
    return header + question + struct.pack("!HH", record_type, 1)


def _read_name(packet, offset):
    labels = []
    cursor = offset
    next_offset = None
    seen = set()
    while True:
        if cursor >= len(packet) or cursor in seen:
            raise ValueError("truncated DNS name")
        seen.add(cursor)
        size = packet[cursor]
        if size == 0:
            return ".".join(labels), (cursor + 1 if next_offset is None else next_offset)
        if size & 0xC0 == 0xC0:
            if cursor + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            pointer = ((size & 0x3F) << 8) | packet[cursor + 1]
            if pointer >= len(packet):
                raise ValueError("invalid DNS pointer")
            if next_offset is None:
                next_offset = cursor + 2
            cursor = pointer
            continue
        if size & 0xC0 or size > 63 or cursor + 1 + size > len(packet):
            raise ValueError("invalid DNS label")
        try:
            labels.append(packet[cursor + 1:cursor + 1 + size].decode("ascii").lower())
        except UnicodeDecodeError:
            raise ValueError("non-ASCII DNS label") from None
        cursor += 1 + size


def _answers(packet, transaction, name, record_type):
    if len(packet) < 12:
        raise ValueError("truncated DNS header")
    response_id, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if response_id != transaction or not flags & 0x8000 or flags & 0x7800 or questions != 1 or answers > 128:
        raise ValueError("invalid DNS response header")
    response_code = flags & 0x000F
    question_name, offset = _read_name(packet, 12)
    if offset + 4 > len(packet):
        raise ValueError("truncated DNS question")
    qtype, qclass = struct.unpack("!HH", packet[offset:offset + 4])
    if question_name != name or qtype != record_type or qclass != 1:
        raise ValueError("unexpected DNS question")
    offset += 4
    for _ in range(questions - 1):
        _, offset = _read_name(packet, offset)
        offset += 4
    addresses, aliases = {}, {}
    for _ in range(answers):
        owner, offset = _read_name(packet, offset)
        if offset + 10 > len(packet):
            raise ValueError("truncated DNS resource record")
        kind, klass, _, length = struct.unpack("!HHIH", packet[offset:offset + 10])
        offset += 10
        if offset + length > len(packet):
            raise ValueError("truncated DNS resource data")
        end = offset + length
        if klass == 1 and kind == record_type and length == (4 if kind == 1 else 16):
            addresses.setdefault(owner, set()).add(str(ipaddress.ip_address(packet[offset:end])))
        elif klass == 1 and kind == 5:
            alias, alias_end = _read_name(packet, offset)
            if alias_end > end:
                raise ValueError("invalid DNS alias")
            aliases[owner] = alias
        offset += length
    found, current, seen = set(), name, set()
    for _ in range(16):
        if current in seen:
            raise ValueError("DNS alias loop")
        seen.add(current)
        if current in addresses:
            found = addresses[current]
            break
        if current not in aliases:
            break
        current = aliases[current]
    return response_code, found


def probe(resolver, name, expected, *, timeout=3, port=53, socket_factory=socket.socket):
    """Return a bounded health result; never retain DNS answers or resolver data."""
    resolver, name, expected = validate(resolver, name, expected)
    if type(timeout) not in (int, float) or not 0 < timeout <= 10 or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid DNS probe policy")
    family = socket.AF_INET6 if ":" in resolver else socket.AF_INET
    for record_type, version in ((1, 4), (28, 6)):
        wanted = {item for item in expected if ipaddress.ip_address(item).version == version}
        if not wanted:
            continue
        transaction = secrets.randbits(16)
        packet = _query_packet(name, record_type, transaction)
        address = (resolver, port, 0, 0) if family == socket.AF_INET6 else (resolver, port)
        try:
            with socket_factory(family, socket.SOCK_DGRAM) as client:
                client.settimeout(timeout)
                client.connect(address)
                client.send(packet)
                response = client.recv(4096)
            response_code, actual = _answers(response, transaction, name, record_type)
        except (OSError, TimeoutError):
            return {"state": "failed", "reason": "dns-unreachable"}
        except ValueError:
            return {"state": "unknown", "reason": "dns-invalid-response"}
        if response_code:
            reason = "dns-nxdomain" if response_code == 3 else "dns-server-error"
            return {"state": "failed", "reason": reason}
        if not wanted.issubset(actual):
            return {"state": "failed", "reason": "dns-answer-mismatch"}
    return {"state": "healthy", "reason": "dns-answer-expected"}
