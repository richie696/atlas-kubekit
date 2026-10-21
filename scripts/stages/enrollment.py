#!/usr/bin/env python3
"""Shared private-network pairing protocol for deployment and auditing."""

import argparse
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path


class PairingError(ValueError):
    pass


def decode_pairing_code(code):
    alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
    code = code.strip().upper().replace('-', '').replace('O', '0').replace('I', '1').replace('L', '1')
    if len(code) != 16 or any(char not in alphabet for char in code):
        raise PairingError('Enter a 16-character pairing code, for example XXXX-XXXX-XXXX-XXXX')
    value = 0
    for char in code:
        value = value * 32 + alphabet.index(char)
    packed = value.to_bytes(10, 'big')
    return str(ipaddress.IPv4Address(packed[:4])), packed[4:].hex()


def enroll(address, code, public_key, expected_name=None):
    """Authorize one public key and authenticate the returned SSH host key."""
    try:
        ipaddress.IPv4Address(address)
        if not re.fullmatch(r'(?:[0-9a-fA-F]{12}|[0-9a-fA-F]{24})', code):
            raise PairingError('Pairing secret must contain 12 hexadecimal characters')
        secret = bytes.fromhex(code)
        key = public_key.strip()
        if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', key):
            raise PairingError('Pairing requires an ED25519 public key')
        nonce = secrets.token_hex(16)
        mac = hmac.new(secret, (nonce + '\n' + key).encode(), hashlib.sha256).hexdigest()
        request = urllib.request.Request(
            f'http://{address}:25422/enroll',
            data=json.dumps({'nonce': nonce, 'key': key, 'mac': mac}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        # Private-network pairing must not be sent through an HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=8) as response:
            reply = json.load(response)
        name, host_key = reply['name'], reply['host_key']
        expected = hmac.new(secret, ('OK\n' + nonce + '\n' + name + '\n' + host_key).encode(), hashlib.sha256).hexdigest()
        if reply['nonce'] != nonce or not hmac.compare_digest(expected, reply['mac']):
            raise PairingError('Enrollment response authentication failed')
        if not re.fullmatch(r'(?:lb|cp|w)[1-9][0-9]*', name):
            raise PairingError('Invalid enrolled hostname')
        if expected_name and name != expected_name:
            raise PairingError(f'Pairing code belongs to {name}; expected {expected_name}')
        if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?', host_key):
            raise PairingError('Invalid enrolled SSH host key')
        return name, host_key
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise PairingError('Invalid pairing code; run 01-prepare-node.sh --code on the target and retry') from None
        raise PairingError(f'Enrollment service returned HTTP {exc.code}; check its service log') from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PairingError(f'Cannot reach enrollment on {address}:25422; run 01-prepare-node.sh --pair on the target') from None
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PairingError('Invalid enrollment response') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    decode = commands.add_parser('decode')
    decode.add_argument('code')
    register = commands.add_parser('enroll')
    register.add_argument('--address', required=True)
    register.add_argument('--code', required=True)
    register.add_argument('--public-key-file', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'decode':
            result = decode_pairing_code(args.code)
        else:
            result = enroll(args.address, args.code, Path(args.public_key_file).read_text())
        print('\t'.join(result))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
