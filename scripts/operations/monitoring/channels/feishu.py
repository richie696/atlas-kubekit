# SPDX-License-Identifier: MIT
"""Feishu custom-bot text contract, without network or credential storage."""
import base64
import hashlib
import hmac
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_BYTES = 20000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def text_payload(text, timestamp=None, secret=None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("invalid message")
    result = {"msg_type": "text", "content": {"text": text}}
    if secret is not None:
        if not isinstance(secret, str) or not secret or type(timestamp) is not int or timestamp < 1:
            raise ValueError("invalid signing configuration")
        key = (str(timestamp) + "\n" + secret).encode("utf-8")
        result["timestamp"] = str(timestamp)
        result["sign"] = base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")
    elif timestamp is not None:
        raise ValueError("timestamp without secret")
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_BYTES:
        raise ValueError("message too large")
    return result


def classify(status, body):
    """Return a safe outcome without retaining vendor responses or credentials."""
    if status is None or type(status) is not int or status >= 500:
        return "unknown"
    if status == 429:
        return "retryable"
    if not 200 <= status < 300:
        return "rejected"
    try:
        if not isinstance(body, (bytes, str)) or len(body) > MAX_BYTES:
            return "unknown"
        data = json.loads(body)
        code = data["code"]
        if type(code) is not int:
            return "unknown"
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return "unknown"
    if code == 0:
        return "accepted"
    return "retryable" if code == 11232 else "rejected"


def validate_endpoint(url):
    """Accept only the official Feishu custom-bot endpoint."""
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid channel endpoint")
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "open.feishu.cn" or
                parts.port not in (None, 443) or parts.username is not None or
                parts.password is not None or parts.query or parts.fragment or
                not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9-]+", parts.path)):
            raise ValueError("invalid channel endpoint")
    except ValueError:
        raise ValueError("invalid channel endpoint") from None
    return url


def deliver(url, payload, opener=None):
    """Send to the official HTTPS endpoint; never return the URL or response body."""
    validate_endpoint(url)
    if (not isinstance(payload, dict) or set(payload) not in
            ({"msg_type", "content"}, {"msg_type", "content", "timestamp", "sign"}) or
            payload.get("msg_type") != "text" or not isinstance(payload.get("content"), dict) or
            set(payload["content"]) != {"text"} or not isinstance(payload["content"]["text"], str) or
            not payload["content"]["text"].strip()):
        raise ValueError("invalid payload")
    if "sign" in payload and (not isinstance(payload["timestamp"], str) or
                              not payload["timestamp"].isdigit() or
                              not isinstance(payload["sign"], str) or
                              not re.fullmatch(r"[A-Za-z0-9+/]{43}=", payload["sign"])):
        raise ValueError("invalid payload")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("message too large")
    transport = opener or build_opener(ProxyHandler({}), _NoRedirect())
    try:
        request = Request(url, data=encoded, headers={"Content-Type": "application/json"}, method="POST")
        with transport.open(request, timeout=10) as response:
            return classify(response.status, response.read(MAX_BYTES + 1))
    except HTTPError as error:
        try:
            return classify(error.code, b"")
        finally:
            error.close()
    except (OSError, URLError, TimeoutError):
        return "unknown"
