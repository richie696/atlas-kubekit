# SPDX-License-Identifier: MIT
"""Render the bounded Prometheus settings used by the KEDA add-on."""
import argparse
import re
import sys


def monitoring_values(prometheus_release=None):
    """Expose Operator metrics and create a selected ServiceMonitor when available."""
    if (prometheus_release is not None and
            (not isinstance(prometheus_release, str) or
             not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", prometheus_release) or
             len(prometheus_release) > 63)):
        raise ValueError("invalid Prometheus release name")
    values = (
        "prometheus:\n"
        "  operator:\n"
        "    enabled: true\n"
        "    serviceMonitor:\n"
        "      enabled: " + ("true\n" if prometheus_release else "false\n")
    )
    if prometheus_release:
        values += (
            "      additionalLabels:\n"
            "        release: " + prometheus_release + "\n"
        )
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-release")
    args = parser.parse_args()
    try:
        sys.stdout.write(monitoring_values(args.prometheus_release))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
