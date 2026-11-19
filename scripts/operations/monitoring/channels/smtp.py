# SPDX-License-Identifier: MIT
"""Send one localized, HTML-only notification through a Secret-backed SMTP route."""
from email.message import EmailMessage
from email.utils import parseaddr
from html import escape
import re
import smtplib
import ssl

from scripts.lib.i18n import available_locales, translate

_HOST = re.compile(r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_MAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9.-]{1,253}\Z")
MAX_TEXT_BYTES = 16384


def _address(value):
    if (not isinstance(value, str) or len(value) > 254 or not _MAIL.fullmatch(value) or
            parseaddr(value)[1] != value or any(ord(char) < 32 for char in value)):
        raise ValueError("invalid SMTP address")
    return value


def _settings(value):
    fields = {"host", "port", "sender", "recipient", "username", "language"}
    if (not isinstance(value, dict) or set(value) != fields or
            not isinstance(value["host"], str) or not _HOST.fullmatch(value["host"]) or
            type(value["port"]) is not int or value["port"] not in (465, 587) or
            value["language"] not in available_locales()):
        raise ValueError("invalid SMTP route")
    result = dict(value)
    result["sender"] = _address(value["sender"])
    result["recipient"] = _address(value["recipient"])
    result["username"] = _address(value["username"])
    return result


def render(notification, text, settings):
    """Render escaped alert text into a compact card; no untrusted markup is emitted."""
    settings = _settings(settings)
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("invalid SMTP message")
    status = getattr(notification, "status", None)
    if status not in ("firing", "resolved"):
        raise ValueError("invalid alert status")
    lang = settings["language"]
    state = translate(lang, "ops.monitoring.mail_" + status)
    title = translate(lang, "ops.monitoring.mail_notification_title")
    subject = "[" + state + "] " + title
    color = "#c94f3d" if status == "firing" else "#67a83d"
    escaped = escape(text, quote=True).replace("\n", "<br>\n")
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body bgcolor="#f3f4f6" style="margin:0;padding:24px 12px;background:#f3f4f6;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;color:#242424">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center">'
        '<table role="presentation" width="640" cellspacing="0" cellpadding="0" '
        'style="width:100%;max-width:640px;background:#fff;border:1px solid #ddd;border-radius:8px;overflow:hidden">'
        '<tr><td style="padding:22px 26px;background:' + color + ';color:#fff;font-size:20px;font-weight:600">'
        + escape(state) + '</td></tr>'
        '<tr><td style="padding:24px 28px"><div style="border:1px solid #e3e5e8;border-radius:6px;'
        'padding:20px;background:#fafbfc;font-size:14px;line-height:1.8;overflow-wrap:anywhere">'
        + escaped + '</div></td></tr>'
        '<tr><td style="padding:18px 24px;text-align:center;border-top:1px solid #eee;'
        'font-size:12px;color:#888">' + escape(title) + '</td></tr>'
        '</table></td></tr></table></body></html>'
    )
    return subject, html


def deliver(notification, text, settings, password, smtp_factory=None, ssl_context=None):
    """Send using implicit TLS (465) or verified STARTTLS (587), returning a safe outcome."""
    settings = _settings(settings)
    if not isinstance(password, str) or not password or len(password) > 8192 or any(ord(c) < 32 for c in password):
        raise ValueError("invalid SMTP credential")
    subject, body = render(notification, text, settings)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings["sender"]
    message["To"] = settings["recipient"]
    message.set_content(body, subtype="html", charset="utf-8")
    factory = smtp_factory or (smtplib.SMTP_SSL if settings["port"] == 465 else smtplib.SMTP)
    context = ssl_context or ssl.create_default_context()
    try:
        if settings["port"] == 465:
            with factory(settings["host"], settings["port"], timeout=10, context=context) as client:
                client.login(settings["username"], password)
                client.send_message(message)
        else:
            with factory(settings["host"], settings["port"], timeout=10) as client:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
                client.login(settings["username"], password)
                client.send_message(message)
        return "accepted"
    except (OSError, smtplib.SMTPException, TimeoutError):
        # Provider exceptions may contain addresses or authentication details.
        return "retryable"
