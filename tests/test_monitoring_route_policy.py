# SPDX-License-Identifier: MIT
import unittest

from scripts.operations.arguments import parser_for
from scripts.operations.models import OperationError
from scripts.operations.monitoring import alert_routing, email_publication, relay_publication


class AlertRoutingTest(unittest.TestCase):
    def test_defaults_and_exact_team_matcher_render_for_email_and_robot(self):
        policy = alert_routing.route_policy(["team=platform"])
        expected = {
            "matchers": [{"name": "severity", "value": "warning", "matchType": "="},
                         {"name": "team", "value": "platform", "matchType": "="}],
            "groupBy": ["cluster", "alertname", "namespace", "severity"],
            "groupWait": "30s", "groupInterval": "5m", "repeatInterval": "12h",
        }
        email = email_publication.manifest("monitoring", "stack", "0.28.1", "smtp.example.test", 465,
            "sender@example.test", "ops@example.test", "sender@example.test", "smtp-auth", "password",
            "warning", "zh_CN", policy)
        robot = relay_publication.alertmanager_config("monitoring", "stack", "ops-wecom", "warning",
            "relay-auth", "token", policy)
        for manifest in (email, robot):
            with self.subTest(kind=manifest["kind"]):
                route = manifest["spec"]["route"]
                for key, value in expected.items():
                    self.assertEqual(route[key], value)

    def test_parser_supports_repeatable_owner_routing_and_group_values(self):
        args = parser_for("en").parse_args([
            "--monitoring-route-matcher", "team=platform",
            "--monitoring-route-matcher", "cluster!=lab",
            "--monitoring-group-by", "cluster", "--monitoring-group-by", "alertname",
            "--monitoring-group-wait", "45s", "--monitoring-group-interval", "3m",
            "--monitoring-repeat-interval", "6h",
        ])
        policy = alert_routing.from_arguments(args)
        self.assertEqual(policy["matchers"], [
            {"name": "cluster", "value": "lab", "matchType": "!="},
            {"name": "team", "value": "platform", "matchType": "="},
        ])
        self.assertEqual(policy["groupBy"], ["cluster", "alertname"])
        self.assertEqual(policy["groupWait"], "45s")

    def test_rejects_untrusted_labels_invalid_durations_and_unsafe_matchers(self):
        for matchers in (["severity=critical"], ["__name__=foo"], ["team=platform", "team=ops"],
                         ["team=platform\nroute"]):
            with self.subTest(matchers=matchers), self.assertRaises(ValueError):
                alert_routing.route_policy(matchers)
        for kwargs in ({"group_by": ["namespace"]}, {"group_wait": "1s"},
                       {"group_interval": "10s"}, {"repeat_interval": "2m"},
                       {"group_wait": "60s", "group_interval": "30s"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                alert_routing.route_policy(**kwargs)

    def test_email_plan_parameters_require_closed_route_policy_shape(self):
        policy = alert_routing.route_policy()
        candidate = {"matchers": policy["matchers"], "groupBy": policy["groupBy"],
                     "groupWait": policy["groupWait"], "groupInterval": policy["groupInterval"],
                     "repeatInterval": policy["repeatInterval"], "unexpected": "ignored"}
        from scripts.operations import configuration
        from scripts.operations.capabilities import monitoring_email
        value = {"configuration": configuration.template(), "namespace": "monitoring", "release": "stack",
                 "managerName": "stack", "version": "0.28.1", "smtpHost": "smtp.example.test",
                 "smtpPort": 465, "sender": "sender@example.test", "recipient": "ops@example.test",
                 "username": "sender@example.test", "secretName": "smtp-auth", "secretKey": "password",
                 "severity": "warning", "language": "zh_CN", "routePolicy": candidate,
                 "expected": {"manager": {"uid": "uid", "specHash": "hash"},
                              "credential": {"uid": "uid", "resourceVersion": "1"}, "config": None}}
        with self.assertRaises(OperationError):
            monitoring_email.parameters(value)


if __name__ == "__main__":
    unittest.main()
