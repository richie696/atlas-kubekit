# SPDX-License-Identifier: MIT
"""Authenticated Alertmanager webhook and separate Pod-IP metrics listener.

Run alongside Alertmanager in the same network namespace. Route metadata contains
environment variable names, while destination credentials are injected through
Secret-backed environment variables. The service performs no persistence:
Alertmanager owns retry; an ambiguous delivery can duplicate.
"""
import argparse
import hmac
import ipaddress
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Mapping
from . import alertmanager, dispatch, secret_file
from scripts.lib.i18n import available_locales
from .. import log_diagnostics


def make_server(address, port, token, deliver):
    try:
        ip = ipaddress.ip_address(address)
        if ip.version != 4 or not ip.is_loopback:
            raise ValueError()
    except ValueError:
        raise ValueError("relay requires a loopback IP") from None
    if (type(port) is not int or not 0 <= port <= 65535 or not isinstance(token, str) or
            not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) or not callable(deliver)):
        raise ValueError("invalid relay configuration")
    authorization = ("Bearer " + token).encode("ascii")
    counts = {"accepted": 0, "unknown": 0, "retryable": 0, "rejected": 0}
    counts_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def reply(self, code):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def authenticated(self):
            headers = self.headers.get_all("Authorization", [])
            supplied = headers[0].encode("ascii") if len(headers) == 1 else b""
            return hmac.compare_digest(supplied, authorization)

        def do_GET(self):
            try:
                if self.path == "/-/healthy":
                    self.reply(200)
                    return
                if not self.authenticated():
                    self.reply(401)
                    return
                if self.path != "/metrics":
                    self.reply(404)
                    return
                with counts_lock:
                    snapshot = dict(counts)
                body = ("# TYPE atlas_relay_deliveries_total counter\n" + "".join(
                    'atlas_relay_deliveries_total{outcome="' + key + '"} ' + str(value) + "\n"
                    for key, value in snapshot.items())).encode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, UnicodeError):
                self.close_connection = True

        def do_POST(self):
            try:
                if not self.authenticated():
                    self.reply(401)
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (self.path != "/v1/alert" or len(lengths) != 1 or
                        self.headers.get("Transfer-Encoding") is not None or
                        not re.fullmatch(r"[0-9]{1,6}", lengths[0])):
                    self.reply(400)
                    return
                size = int(lengths[0])
                if not 1 <= size <= alertmanager.MAX_BODY:
                    self.reply(413)
                    return
                body = self.rfile.read(size)
                if len(body) != size:
                    self.reply(400)
                    return
                result = deliver(body)
                if result not in counts:
                    result = "unknown"
                with counts_lock:
                    counts[result] += 1
                self.reply(alertmanager.response_code(result))
            except (ValueError, UnicodeError):
                with counts_lock:
                    counts["rejected"] += 1
                self.reply(400)
            except (OSError, TimeoutError):
                self.close_connection = True
            except Exception:
                # Never return an exception or provider response containing secrets.
                with counts_lock:
                    counts["unknown"] += 1
                self.reply(503)

    server = HTTPServer((address, port), Handler)
    server.metrics_counts = counts
    server.metrics_counts_lock = counts_lock
    server.metrics_authorization = authorization
    return server


def make_metrics_server(address, port, receiver_server):
    """Expose authenticated metrics on the Pod IP without exposing the webhook."""
    if (address != "0.0.0.0" or type(port) is not int or not 0 <= port <= 65535 or
            not isinstance(receiver_server, HTTPServer)):
        raise ValueError("invalid relay metrics listener")
    counts = receiver_server.metrics_counts
    counts_lock = receiver_server.metrics_counts_lock
    authorization = receiver_server.metrics_authorization

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass

        def reply(self, code):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def authenticated(self):
            headers = self.headers.get_all("Authorization", [])
            supplied = headers[0].encode("ascii") if len(headers) == 1 else b""
            return hmac.compare_digest(supplied, authorization)

        def do_GET(self):
            if not self.authenticated():
                self.reply(401)
                return
            if self.path != "/metrics":
                self.reply(404)
                return
            with counts_lock:
                snapshot = dict(counts)
            body = ("# TYPE atlas_relay_deliveries_total counter\n" + "".join(
                'atlas_relay_deliveries_total{outcome="' + key + '"} ' + str(value) + "\n"
                for key, value in snapshot.items())).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.reply(404)

    return ThreadingHTTPServer((address, port), Handler)


def route_dispatch(config, environment, opener=None, now=None):
    """Build one destination dispatcher from non-secret route config and secret env values."""
    if (not isinstance(config, dict) or set(config) - {"version", "routes", "diagnostics"} or
            not {"version", "routes"} <= set(config) or
            type(config["version"]) is not int or config["version"] != 1 or
            not isinstance(config["routes"], list) or not 1 <= len(config["routes"]) <= 100 or
            not isinstance(environment, Mapping)):
        raise ValueError("invalid relay route configuration")
    diagnostics = config.get("diagnostics")
    if diagnostics is not None:
        try:
            diagnostics = log_diagnostics.validate_settings(diagnostics)
        except (TypeError, ValueError):
            raise ValueError("invalid diagnostics configuration") from None
    routes = {}
    env_name = re.compile(r"ATLAS_RELAY_[A-Z0-9_]{1,64}\Z")
    for item in config["routes"]:
        if (not isinstance(item, dict) or
                not {"receiver", "kind", "language"} <= set(item) or
                not isinstance(item["receiver"], str) or not 1 <= len(item["receiver"]) <= 256 or
                any(ord(char) < 32 or ord(char) == 127 for char in item["receiver"]) or
                item["kind"] not in ("wecom", "dingtalk", "feishu", "email") or
                not isinstance(item["language"], str) or
                item["language"] not in available_locales() or
                item["receiver"] in routes):
            raise ValueError("invalid relay route configuration")
        if item["kind"] == "email":
            expected = {"receiver", "kind", "language", "password_env", "smtp"}
            smtp = item.get("smtp")
            if (set(item) != expected or not isinstance(item["password_env"], str) or
                    not env_name.fullmatch(item["password_env"]) or not isinstance(smtp, dict) or
                    set(smtp) != {"host", "port", "sender", "recipient", "username", "language"} or
                    smtp.get("language") != item["language"]):
                raise ValueError("invalid relay email route")
            password = environment.get(item["password_env"])
            if not isinstance(password, str) or not password or len(password) > 8192 or any(ord(c) < 32 for c in password):
                raise ValueError("invalid relay email credentials")
            try:
                from .smtp import _settings
                smtp_settings = _settings(smtp)
            except (TypeError, ValueError):
                raise ValueError("invalid relay email route") from None
            routes[item["receiver"]] = ("email", item["language"], None, password, smtp_settings)
            continue
        if (set(item) - {"receiver", "kind", "language", "endpoint_env", "secret_env"} or
                not {"receiver", "kind", "language", "endpoint_env"} <= set(item) or
                item["kind"] not in ("wecom", "dingtalk", "feishu") or
                not isinstance(item["endpoint_env"], str) or not env_name.fullmatch(item["endpoint_env"]) or
                ("secret_env" in item and (not isinstance(item["secret_env"], str) or
                                           not env_name.fullmatch(item["secret_env"])))):
            raise ValueError("invalid relay route configuration")
        endpoint = environment.get(item["endpoint_env"])
        secret = environment.get(item.get("secret_env")) if item.get("secret_env") else None
        if (not isinstance(endpoint, str) or not endpoint or len(endpoint) > 8192 or
                any(ord(char) < 32 or ord(char) == 127 for char in endpoint) or
                (item.get("secret_env") and (not isinstance(secret, str) or not secret or len(secret) > 8192 or
                                              any(ord(char) < 32 or ord(char) == 127 for char in secret)))):
            raise ValueError("invalid relay route credentials")
        routes[item["receiver"]] = (item["kind"], item["language"], endpoint, secret, None)

    def deliver(body):
        notification = alertmanager.parse(body, "zh_CN")
        route_receiver = notification.receiver.rsplit("/", 1)[-1]
        route = routes.get(route_receiver)
        if route is None:
            raise ValueError("unknown relay receiver")
        kind, lang, endpoint, secret, smtp_settings = route
        return dispatch.dispatch(body, route_receiver, kind, endpoint, secret, opener, now, lang, diagnostics,
                                 opener if diagnostics is not None else None, smtp_settings)
    return deliver


def _route_file(path):
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("invalid relay route file")
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise ValueError()
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid relay route file") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--metrics-address", default="0.0.0.0")
    parser.add_argument("--metrics-port", type=int, default=18081)
    parser.add_argument("--receiver")
    parser.add_argument("--kind", choices=("wecom", "dingtalk", "feishu"))
    parser.add_argument("--lang", choices=tuple(available_locales()), default="zh_CN")
    parser.add_argument("--token-file")
    parser.add_argument("--token-env", default="ATLAS_RELAY_TOKEN")
    parser.add_argument("--routes-file")
    parser.add_argument("--endpoint-file")
    parser.add_argument("--signing-secret-file")
    args = parser.parse_args()
    try:
        if args.routes_file:
            if args.receiver or args.kind or args.endpoint_file or args.signing_secret_file or args.token_file:
                raise ValueError()
            if not re.fullmatch(r"ATLAS_RELAY_[A-Z0-9_]{1,64}", args.token_env):
                raise ValueError()
            token = os.environ.get(args.token_env)
            deliver = route_dispatch(_route_file(args.routes_file), os.environ)
        else:
            if not args.receiver or not args.kind or not args.endpoint_file or not args.token_file:
                raise ValueError()
            token = secret_file.read_private(args.token_file)
            endpoint = secret_file.read_private(args.endpoint_file)
            secret = secret_file.read_private(args.signing_secret_file) if args.signing_secret_file else None
            def deliver(body):
                return dispatch.dispatch(body, args.receiver, args.kind, endpoint, secret, lang=args.lang)
        server = make_server(args.address, args.port, token, deliver)
        metrics_server = make_metrics_server(args.metrics_address, args.metrics_port, server)
        metrics_thread = threading.Thread(target=metrics_server.serve_forever, daemon=True)
        metrics_thread.start()
        try:
            server.serve_forever()
        finally:
            server.server_close()
            metrics_server.shutdown()
            metrics_server.server_close()
            metrics_thread.join(timeout=5)
    except (ValueError, OSError):
        parser.exit(1, "Invalid relay configuration or inaccessible credential file.\n")


if __name__ == "__main__":
    main()
