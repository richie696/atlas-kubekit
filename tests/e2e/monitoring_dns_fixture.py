# SPDX-License-Identifier: MIT
"""Private UDP DNS fixture for the disposable CoreDNS regression."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import struct
import sys
import time
import uuid

ADDRESS = socket.inet_aton("192.0.2.7")
MODE = Path("/tmp/atlas-dns-mode")
HEADER = struct.Struct("!6H")


class Upstream(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
        self.loop = asyncio.get_running_loop()

    def datagram_received(self, packet, peer):
        transaction, flags, questions, *_ = HEADER.unpack_from(packet)
        if questions != 1:
            return
        end = HEADER.size
        while packet[end]:
            end += packet[end] + 1
        end += 5
        mode = MODE.read_text() if MODE.exists() else "normal"
        rcode = 2 if mode == "servfail" else 0
        answer = b"" if rcode else b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 0, 4) + ADDRESS
        reply = (HEADER.pack(transaction, 0x8080 | (flags & 0x0100) | rcode, 1, int(not rcode), 0, 0)
                 + packet[HEADER.size:end] + answer)
        self.loop.call_later(0.35 if mode == "slow" else 0, self.transport.sendto, reply, peer)


async def serve():
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Upstream, local_addr=("0.0.0.0", 1053))
    try:
        await asyncio.Future()
    finally:
        transport.close()


def ask(resolver, name):
    labels = b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split("."))
    transaction = int.from_bytes(uuid.uuid4().bytes[:2], "big")
    query = HEADER.pack(transaction, 0x0100, 1, 0, 0, 0) + labels + b"\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(3)
        client.connect((resolver, 53))
        start = time.monotonic()
        client.send(query)
        packet = client.recv(512)
        elapsed = time.monotonic() - start
    response_id, flags, _, answers, *_ = HEADER.unpack_from(packet)
    if response_id != transaction or not (flags & 0x8000):
        raise RuntimeError("unexpected DNS response")
    return flags & 0x000F, answers == 1 and packet.endswith(ADDRESS), elapsed


def probe(zone):
    resolver = next(line.split()[1] for line in Path("/etc/resolv.conf").read_text().splitlines()
                    if line.startswith("nameserver "))
    names = [uuid.uuid4().hex + "." + zone for _ in range(32)]
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda name: ask(resolver, name), names))
    return {"resolver": resolver, "queries": len(results), "rcodes": sorted({row[0] for row in results}),
            "valid_answers": all(row[1] for row in results),
            "minimum_seconds": min(row[2] for row in results)}


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        asyncio.run(serve())
    elif sys.argv[1] == "mode" and sys.argv[2] in ("normal", "servfail", "slow"):
        pending = MODE.with_suffix(".new")
        pending.write_text(sys.argv[2])
        pending.replace(MODE)
    elif sys.argv[1] == "probe":
        print(json.dumps(probe(sys.argv[2])))
    else:
        raise SystemExit("invalid fixture command")
