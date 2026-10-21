#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Shared message-key localization catalog loader for Atlas KubeKit."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LOCALES_DIR = Path(os.environ.get("ATLAS_LOCALES_DIR", ROOT / "locales"))
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
FORMAT_PATTERN = re.compile(r"%(?:\([^)]+\))?[#0 +\-]?(?:\d+|\*)?(?:\.(?:\d+|\*))?[diouxXeEfFgGcrs%]")


class LocalizationError(ValueError):
    """Raised for invalid locale catalogs or unknown locale codes."""


def available_locales() -> dict[str, str]:
    """Return supported locale codes and display names, ordered by code."""
    result: dict[str, str] = {}
    for path in sorted(LOCALES_DIR.glob("*.json")):
        catalog = _read(path.stem)
        result[catalog["locale"]] = catalog["displayName"]
    return result


def _locale_path(locale: str) -> Path:
    normalized = locale.replace("-", "_").casefold()
    for path in LOCALES_DIR.glob("*.json"):
        if path.stem.replace("-", "_").casefold() == normalized:
            return path
    raise LocalizationError(f"Unsupported locale: {locale}")


def _read(locale: str) -> dict[str, Any]:
    try:
        path = _locale_path(locale)
    except LocalizationError:
        raise LocalizationError(f"Unsupported locale: {locale}") from None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LocalizationError(f"Unsupported locale: {locale}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise LocalizationError(f"Cannot read locale {locale}: {error}") from error
    actual = str(data.get("locale", "")).replace("-", "_").casefold() if isinstance(data, dict) else ""
    if not isinstance(data, dict) or actual != path.stem.replace("-", "_").casefold():
        raise LocalizationError(f"Invalid locale metadata: {path}")
    if not isinstance(data.get("displayName"), str) or not isinstance(data.get("messages"), dict):
        raise LocalizationError(f"Invalid locale structure: {path}")
    for key, value in data["messages"].items():
        if not KEY_PATTERN.fullmatch(key) or not isinstance(value, str):
            raise LocalizationError(f"Invalid message entry in {path}: {key!r}")
    return data


def _chain(locale: str) -> list[dict[str, Any]]:
    catalogs: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = locale
    while current:
        catalog = _read(current)
        canonical = catalog["locale"].replace("-", "_").casefold()
        if canonical in seen:
            raise LocalizationError(f"Locale fallback cycle: {current}")
        seen.add(canonical)
        catalogs.append(catalog)
        current = catalog.get("fallback")
        if current is not None and not isinstance(current, str):
            raise LocalizationError(f"Invalid fallback in locale {catalog['locale']}")
    if "en" not in seen:
        catalogs.append(_read("en"))
    return catalogs


def translate(locale: str, message_key: str, *values: object) -> str:
    """Resolve a message key through its fallback chain and interpolate values."""
    if not KEY_PATTERN.fullmatch(message_key):
        raise LocalizationError(f"Invalid message key: {message_key!r}")
    message = None
    for catalog in _chain(locale):
        candidate = catalog["messages"].get(message_key)
        if candidate is not None:
            message = candidate
            break
    if message is None:
        raise LocalizationError(f"Message key is missing from locale fallback chain: {message_key}")
    if values:
        try:
            message = message % values
        except (TypeError, ValueError) as error:
            raise LocalizationError(f"Invalid format values for {message_key}: {error}") from error
    return message


def export_bash(locale: str) -> str:
    """Emit shell-quoted assignments for one-time loading into Bash."""
    rows = ["declare -gA ATLAS_I18N_MESSAGES=()"]
    for catalog in reversed(_chain(locale)):
        for key, value in catalog["messages"].items():
            rows.append(f"ATLAS_I18N_MESSAGES[{shlex.quote(key)}]={shlex.quote(value)}")
    return "\n".join(rows) + "\n"


def validate_catalogs() -> list[str]:
    """Validate locale metadata, fallback chains, message keys, and placeholders."""
    errors: list[str] = []
    catalogs = [_read(path.stem) for path in sorted(LOCALES_DIR.glob("*.json"))]
    known = {catalog["locale"] for catalog in catalogs}
    for catalog in catalogs:
        code = catalog["locale"]
        fallback = catalog.get("fallback")
        if fallback is not None and fallback not in known:
            errors.append(f"{code}: fallback {fallback!r} does not exist")
        try:
            _chain(code)
        except LocalizationError as error:
            errors.append(str(error))
    english = _read("en")["messages"]
    for catalog in catalogs:
        code = catalog["locale"].replace("-", "_")
        messages = catalog["messages"]
        extra = sorted(set(messages) - set(english))
        if extra:
            errors.append(f"{code}: keys absent from en.json: {', '.join(extra)}")
        for key in sorted(set(messages) & set(english)):
            expected = sorted(FORMAT_PATTERN.findall(english[key]))
            actual = sorted(FORMAT_PATTERN.findall(messages[key]))
            if expected != actual:
                errors.append(f"{code}: printf placeholders differ for {key}")
    return errors


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list locale codes and display names")
    parser.add_argument("--validate", action="store_true", help="validate locale catalogs")
    parser.add_argument("--bash", metavar="LOCALE", help="emit shell-quoted Bash assignments")
    parser.add_argument("locale", nargs="?")
    parser.add_argument("message_key", nargs="?")
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    try:
        if args.list:
            for code, display_name in available_locales().items():
                print(f"{code}\t{display_name}")
        elif args.bash:
            print(export_bash(args.bash), end="")
        elif args.validate:
            errors = validate_catalogs()
            for error in errors:
                print(error, file=sys.stderr)
            if errors:
                raise SystemExit(1)
            print("Locale catalog structure is valid")
        elif args.locale and args.message_key:
            print(translate(args.locale, args.message_key, *args.values))
        else:
            parser.error("provide --list, --validate, or LOCALE MESSAGE_KEY [VALUES...]")
    except LocalizationError as error:
        print(f"i18n: {error}", file=sys.stderr)
        raise SystemExit(2) from error
