# SPDX-License-Identifier: MIT
"""Localized argument contracts, independent of command execution."""
import argparse
import sys
from pathlib import Path
from scripts.lib.i18n import translate, available_locales
from .capabilities.registry import CAPABILITIES


def parser_for(lang):
    tr = lambda k: translate(lang, k)

    class Help(argparse.HelpFormatter):
        def _format_usage(self, usage, actions, groups, prefix):
            return super()._format_usage(usage, actions, groups, tr("ops.cli.usage") + ": ")

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            self.print_usage(sys.stderr)
            self.exit(2, tr("ops.error.arguments") + "\n")

    parser = Parser(description=tr("ops.cli.description"), add_help=False, formatter_class=Help)
    parser._optionals.title = tr("ops.cli.options")
    parser.add_argument("-h", "--help", action="help", help=tr("ops.cli.help"))
    parser.add_argument(
        "--lang", default=lang, choices=list(available_locales()), help=tr("ops.cli.lang")
    )
    group = parser.add_mutually_exclusive_group()
    for name in (
        "report",
        "security-check",
        "backup-plan",
        "backup-create",
        "backup-verify",
        "restore-plan",
        "config-template",
        "config-check",
        "config-import",
        "config-export",
        "config-drift",
        "change-plan",
        "change-continue",
        "change-apply",
        "change-status",
        "lock-status",
        "lock-recover",
        "restore-execute",
        "restore-rehearse",
        "diagnostics",
        "materials-prepare",
        "materials-verify",
        "materials-import",
        "protection-status",
    ):
        group.add_argument(
            "--" + name,
            dest="action",
            action="store_const",
            const=name,
            help=tr("ops.cli." + name.replace("-", "_")),
        )
    parser.add_argument("--target", help=tr("ops.cli.target"))
    parser.add_argument("--sudo", action="store_true", help=tr("ops.cli.sudo"))
    parser.add_argument("--ssh-identity", help=tr("ops.cli.ssh_identity"))
    parser.add_argument(
        "--output-dir",
        default=str(Path.home() / ".local/state/atlas-kubekit/operations"),
        help=tr("ops.cli.output"),
    )
    parser.add_argument(
        "--scope",
        choices=("configuration", "control-plane"),
        default="configuration",
        help=tr("ops.cli.scope"),
    )
    parser.add_argument("--recipient", help=tr("ops.cli.recipient"))
    parser.add_argument("--backup", help=tr("ops.cli.backup"))
    parser.add_argument("--identity", help=tr("ops.cli.identity"))
    parser.add_argument("--restore-target", help=tr("ops.cli.restore_target"))
    parser.add_argument("--confirm", help=tr("ops.cli.confirm"))
    parser.add_argument("--config", help=tr("ops.cli.config"))
    parser.add_argument("--profile-name", help=tr("ops.cli.profile_name"))
    parser.add_argument(
        "--state-dir",
        default=str(Path.home() / ".local/state/atlas-kubekit/configurations"),
        help=tr("ops.cli.state_dir"),
    )
    parser.add_argument("--export-file", help=tr("ops.cli.export_file"))
    parser.add_argument("--expected-revision", help=tr("ops.cli.expected_revision"))
    for name in (
        "ssh-config",
        "control-target",
        "plan-file",
        "plan-id",
        "node",
        "lock-owner",
        "resource-version",
        "source-fingerprint",
        "stats-cidr",
        "retained-user",
        "to-version",
    ):
        parser.add_argument("--" + name, help=tr("ops.cli." + name.replace("-", "_")))
    parser.add_argument(
        "--kubeconfig", default="/etc/kubernetes/admin.conf", help=tr("ops.cli.kubeconfig")
    )
    parser.add_argument(
        "--lock-namespace", default="kube-system", help=tr("ops.cli.lock_namespace")
    )
    parser.add_argument("--operation", choices=list(CAPABILITIES), help=tr("ops.cli.operation"))
    parser.add_argument(
        "--maintenance-action",
        choices=("cordon", "drain", "uncordon"),
        help=tr("ops.cli.maintenance_action"),
    )
    parser.add_argument(
        "--delete-emptydir", action="store_true", help=tr("ops.cli.delete_emptydir")
    )
    parser.add_argument(
        "--bump-revision", type=int, default=1000000000, help=tr("ops.cli.bump_revision")
    )
    for name in (
        "node-address",
        "node-interface",
        "data-device",
        "replacement-name",
        "materials",
        "material-id",
        "registry",
        "ca-file",
        "chart",
        "evidence-file",
    ):
        parser.add_argument("--" + name, help=tr("ops.cli." + name.replace("-", "_")))
    return parser
