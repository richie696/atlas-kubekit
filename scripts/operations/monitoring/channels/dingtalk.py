# SPDX-License-Identifier: MIT
"""DingTalk custom group bot adapter with optional HMAC signing."""
import base64
import hashlib
import hmac
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from . import batching, common


def _endpoint(url, timestamp_ms=None, secret=None):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid channel endpoint")
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        if (parts.scheme != "https" or parts.hostname != "oapi.dingtalk.com" or
                parts.port not in (None, 443) or parts.username is not None or parts.password is not None or
                parts.fragment or parts.path != "/robot/send" or len(query) != 1 or
                query[0][0] != "access_token" or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", query[0][1])):
            raise ValueError()
        if secret is not None:
            if not isinstance(secret, str) or not secret or type(timestamp_ms) is not int or timestamp_ms < 1:
                raise ValueError()
            encoded = secret.encode("utf-8")
            message = (str(timestamp_ms) + "\n" + secret).encode("utf-8")
            signature = base64.b64encode(hmac.new(encoded, message, hashlib.sha256).digest()).decode("ascii")
            query += [("timestamp", str(timestamp_ms)), ("sign", signature)]
        elif timestamp_ms is not None:
            raise ValueError()
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except ValueError:
        raise ValueError("invalid channel endpoint or signing configuration") from None


def validate_endpoint(url):
    """Accept only the official DingTalk custom-bot endpoint."""
    return _endpoint(url)


def text_payload(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("invalid message")
    return {"msgtype": "text", "text": {"content": "atlas | " + text}}


def deliver(url, text, timestamp_ms=None, secret=None, opener=None):
    destination = _endpoint(url, timestamp_ms, secret)
    # text_payload adds the configured keyword to every chunk, including split
    # notifications, so DingTalk keyword verification works for all parts.
    for part in batching.split_text(text, text_payload, 16000):
        status, body = common.send_json(destination, text_payload(part), 16000, opener)
        outcome = common.classify(status, body)
        if outcome != "accepted":
            return outcome
    return "accepted"
