# SPDX-License-Identifier: MIT
"""WeCom group robot adapter; credentials are supplied at execution time."""
import re
from urllib.parse import parse_qsl, urlsplit
from . import batching, common


def _endpoint(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid channel endpoint")
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        if (parts.scheme != "https" or parts.hostname != "qyapi.weixin.qq.com" or
                parts.port not in (None, 443) or parts.username is not None or parts.password is not None or
                parts.fragment or parts.path != "/cgi-bin/webhook/send" or len(query) != 1 or
                query[0][0] != "key" or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", query[0][1])):
            raise ValueError()
    except ValueError:
        raise ValueError("invalid channel endpoint") from None
    return url


def validate_endpoint(url):
    """Accept only the official WeCom group-bot endpoint."""
    return _endpoint(url)


def text_payload(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("invalid message")
    return {"msgtype": "text", "text": {"content": text}}


def deliver(url, text, opener=None):
    endpoint = _endpoint(url)
    for part in batching.split_text(text, text_payload, 2048):
        status, body = common.send_json(endpoint, text_payload(part), 2048, opener)
        outcome = common.classify(status, body)
        if outcome != "accepted":
            return outcome
    return "accepted"
