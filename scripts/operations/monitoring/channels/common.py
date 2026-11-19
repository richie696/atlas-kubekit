# SPDX-License-Identifier: MIT
"""Bounded webhook transport; vendor endpoints and payloads are caller-owned."""
import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_json(url, payload, max_size, opener=None):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_size:
        raise ValueError("message too large")
    client = opener or build_opener(ProxyHandler({}), NoRedirect())
    try:
        request = Request(url, data=encoded, headers={"Content-Type": "application/json"}, method="POST")
        with client.open(request, timeout=10) as response:
            return response.status, response.read(20001)
    except HTTPError as error:
        try:
            return error.code, b""
        finally:
            error.close()
    except (OSError, URLError, TimeoutError):
        return None, b""


def classify(status, body):
    """Only a numeric vendor success code establishes platform acceptance."""
    if type(status) is not int or status >= 500:
        return "unknown"
    if status == 429:
        return "retryable"
    if not 200 <= status < 300:
        return "rejected"
    try:
        if not isinstance(body, (str, bytes)) or len(body) > 20000:
            return "unknown"
        code = json.loads(body)["errcode"]
        if type(code) is not int:
            return "unknown"
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return "unknown"
    return "accepted" if code == 0 else "rejected"
