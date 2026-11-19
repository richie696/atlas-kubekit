# SPDX-License-Identifier: MIT
"""Read-only endpoint probing; a failed route is not proof of global failure."""
import ipaddress
import os
from pathlib import Path
import stat
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 32 for char in url):
        raise ValueError("invalid probe endpoint")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username is not None or
                parts.password is not None or parts.query or parts.fragment or ".." in parts.path.split("/")):
            raise ValueError()
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        if parts.scheme == "http" and not ipaddress.ip_address(parts.hostname).is_loopback:
            raise ValueError()
    except ValueError:
        raise ValueError("invalid probe endpoint") from None
    return url


def validate_ca_file(ca_file):
    """Validate an optional CA bundle without following symlinks."""
    if ca_file is None:
        return None
    if not isinstance(ca_file, str) or not ca_file or not os.path.isabs(ca_file):
        raise ValueError("invalid CA bundle path")
    path = Path(ca_file)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("invalid CA bundle")
        context = ssl.create_default_context(cafile=str(path))
        if not context.get_ca_certs():
            raise ValueError("empty CA bundle")
    except (OSError, ssl.SSLError, ValueError):
        raise ValueError("invalid CA bundle") from None
    return str(path)


def build_probe_opener(ca_file=None):
    """Build one direct, non-redirecting HTTPS client for a probe service."""
    ca_file = validate_ca_file(ca_file)
    context = ssl.create_default_context(cafile=ca_file)
    return build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context))


def probe(url, timeout=5, expected_status=200, opener=None, ca_file=None):
    """Return only state and reason; never return URL, headers or response body."""
    validate_url(url)
    if type(timeout) is not int or not 1 <= timeout <= 30 or type(expected_status) is not int or not 200 <= expected_status <= 299:
        raise ValueError("invalid probe policy")
    if opener is None:
        client = build_probe_opener(ca_file)
    else:
        if ca_file is not None:
            validate_ca_file(ca_file)
        client = opener
    try:
        with client.open(Request(url, method="GET"), timeout=timeout) as response:
            if response.status == expected_status:
                return {"state": "healthy", "reason": "expected-http-status"}
            return {"state": "failed", "reason": "unexpected-http-status"}
    except HTTPError as error:
        try:
            if error.code in (401, 403) or 300 <= error.code < 400:
                return {"state": "unknown", "reason": "authentication-or-redirect"}
            return {"state": "failed", "reason": "unexpected-http-status"}
        finally:
            error.close()
    except ssl.SSLError:
        return {"state": "unknown", "reason": "tls-error"}
    except URLError as error:
        if isinstance(error.reason, ssl.SSLError):
            return {"state": "unknown", "reason": "tls-error"}
        return {"state": "failed", "reason": "unreachable-from-observer"}
    except (OSError, TimeoutError):
        return {"state": "failed", "reason": "unreachable-from-observer"}
