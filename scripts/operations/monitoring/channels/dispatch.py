# SPDX-License-Identifier: MIT
"""Dispatch one authenticated Alertmanager route to one robot destination.

The HTTP boundary and credential loading belong to the caller. One route per
delivery lets Alertmanager retry a failing destination without resending to the
other destinations. A timeout is ambiguous and can result in duplicate messages.
"""
import time
from . import alertmanager, batching, dingtalk, feishu, smtp, wecom
from .. import log_diagnostics, loki


def dispatch(body, receiver, kind, endpoint, secret=None, opener=None, now=None, lang="zh_CN",
             diagnostics=None, loki_opener=None, smtp_settings=None):
    """Return only an outcome, never the endpoint, secret, or provider response."""
    notification = alertmanager.parse(body, lang)
    # Prometheus Operator prefixes AlertmanagerConfig receivers with the
    # namespace and config identity. Route by the configured receiver suffix.
    alertmanager_receiver = notification.receiver.rsplit("/", 1)[-1]
    if not isinstance(receiver, str) or alertmanager_receiver != receiver:
        raise ValueError("receiver does not match route")
    message = notification.text
    if diagnostics is not None:
        message = log_diagnostics.enrich_text(
            notification, diagnostics,
            lambda loki_endpoint, alert, before, after, limit: loki.query(
                loki_endpoint, alert, before, after, limit, loki_opener),
            lang)
    clock = time.time if now is None else now
    if kind == "wecom":
        if secret is not None:
            raise ValueError("invalid channel configuration")
        return wecom.deliver(endpoint, message, opener)
    if kind == "dingtalk":
        timestamp = int(clock() * 1000) if secret is not None else None
        return dingtalk.deliver(endpoint, message, timestamp, secret, opener)
    if kind == "feishu":
        timestamp = int(clock()) if secret is not None else None
        template = feishu.text_payload("placeholder", timestamp, secret)
        def payload(text):
            return {**template, "content": {"text": "atlas | " + text}}
        for part in batching.split_text(message, payload, feishu.MAX_BYTES):
            outcome = feishu.deliver(endpoint, payload(part), opener)
            if outcome != "accepted":
                return outcome
        return "accepted"
    if kind == "email":
        if not isinstance(smtp_settings, dict):
            raise ValueError("invalid SMTP route")
        return smtp.deliver(notification, message, smtp_settings, secret)
    raise ValueError("invalid channel configuration")
