"""Cilium artifact integrity, policy-only finalization and fail-stop rollout boundaries."""

import copy
import gzip
import io
import tarfile
import unittest
from unittest.mock import Mock, patch

from scripts.operations.components import cilium_chart, cilium_policy, cilium_upgrade
from scripts.operations.models import OperationError
from scripts.operations.host_actions import component_images


class CiliumTests(unittest.TestCase):
    def chart(self, duplicate=False):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            metadata = b"name: cilium\nversion: 1.20.4\nappVersion: v1.20.4\n"
            for _ in range(2 if duplicate else 1):
                entry = tarfile.TarInfo("cilium/Chart.yaml")
                entry.size = len(metadata)
                archive.addfile(entry, io.BytesIO(metadata))
        return gzip.compress(stream.getvalue())

    def test_gzip_extra_header_and_duplicate_chart_metadata(self):
        raw = self.chart()
        # Helm-produced gzip archives can have FEXTRA, including on Python 3.9.
        extra = raw[:3] + bytes([raw[3] | 4]) + raw[4:10] + b"\x04\x00ABCD" + raw[10:]
        self.assertEqual(len(cilium_chart.inspect(extra, "1.20.4")), 64)
        for data, target in ((self.chart(True), "1.20.4"), (raw, "1.20.3"), (raw[:20], "1.20.4")):
            with self.assertRaises(OperationError):
                cilium_chart.inspect(data, target)

    def test_rollout_does_not_modify_private_input_or_allow_operator_unavailability(self):
        original = dict(secret="PRIVATE", envoy=dict(podAnnotations=dict(custom="kept")))
        before = copy.deepcopy(original)
        result = cilium_policy.rollout_values(original, "a" * 64)
        self.assertEqual(original, before)
        self.assertEqual(
            result["operator"]["updateStrategy"]["rollingUpdate"],
            dict(maxUnavailable=0, maxSurge=1),
        )
        self.assertEqual(result["envoy"]["updateStrategy"]["type"], "OnDelete")
        result["envoy"]["podAnnotations"]["custom"] = "changed"
        self.assertEqual(original, before)

    def test_finalization_accepts_only_strategy_changes(self):
        daemon = dict(
            apiVersion="apps/v1",
            kind="DaemonSet",
            metadata=dict(name="cilium", namespace="kube-system"),
            spec=dict(
                updateStrategy=dict(type="OnDelete"),
                template=dict(spec=dict(containers=[dict(name="agent", image="pinned")])),
            ),
        )
        after = copy.deepcopy(daemon)
        after["spec"]["updateStrategy"] = dict(
            type="RollingUpdate", rollingUpdate=dict(maxUnavailable=1)
        )
        cilium_policy.policy_only([daemon], [after])
        after["spec"]["template"]["spec"]["containers"][0]["image"] = "unexpected"
        with self.assertRaises(OperationError):
            cilium_policy.policy_only([daemon], [after])
        secret = dict(
            apiVersion="v1", kind="Secret", metadata=dict(name="tls"), data=dict(key="PRIVATE")
        )
        changed = copy.deepcopy(secret)
        changed["data"]["key"] = "NEW"
        with self.assertRaises(OperationError):
            cilium_policy.policy_only([daemon, secret], [daemon, changed])

    def test_pdb_rejection_after_templates_stops_before_any_pod_eviction(self):
        values = dict(
            configuration={},
            rolloutOwner="",
            resumeOf="",
            policies={"cilium": {}, "cilium-envoy": {}},
        )
        host = Mock()
        host.call.return_value = dict(identity={})
        environment = Mock()
        environment.host.return_value = host
        environment.verify.return_value = ({}, {})
        environment.client.get.return_value = dict(items=[])
        facts = dict(
            nodes=[dict(name="cp1"), dict(name="cp2")],
            host=dict(
                release={},
                chartSha="a" * 64,
                rendered=dict(daemons={"cilium": {}, "cilium-envoy": {}}, operator={}),
            ),
        )
        checkpoints = []
        with (
            patch.object(cilium_upgrade, "parameters", return_value=values),
            patch.object(cilium_upgrade, "observe", return_value=facts),
            patch.object(cilium_upgrade, "request", return_value={}),
            patch.object(cilium_upgrade, "healthy_etcd"),
            patch.object(cilium_upgrade, "check_identity"),
            patch.object(cilium_upgrade, "target_pod", return_value=dict(spec={}, metadata={})),
            patch.object(
                cilium_upgrade, "require_pdbs", side_effect=OperationError("ops.lifecycle.pdb")
            ),
        ):
            with self.assertRaises(OperationError):
                cilium_upgrade.apply(values, environment, checkpoints.append, "b" * 64)
        environment.client.evict_pod.assert_not_called()
        self.assertEqual(
            checkpoints,
            ["cilium-images-cp1", "cilium-images-cp2", "cilium-templates", "cilium-envoy-cp1"],
        )
        self.assertEqual(
            [call.args[0] for call in host.call.call_args_list],
            ["component-images", "component-images", "component-apply"],
        )

    def test_image_preparation_failure_precedes_helm_or_eviction(self):
        environment = Mock()
        host = environment.host.return_value
        host.call.side_effect = OperationError("ops.host.command")
        values = dict(configuration={}, rolloutOwner="")
        facts = dict(
            nodes=[dict(name="cp1")],
            host=dict(rendered=dict(daemons={"cilium": {"agent": "image"}}, operator={})),
        )
        checkpoints = []
        with (
            patch.object(cilium_upgrade, "parameters", return_value=values),
            patch.object(cilium_upgrade, "observe", return_value=facts),
        ):
            with self.assertRaises(OperationError):
                cilium_upgrade.apply(values, environment, checkpoints.append, "a" * 64)
        self.assertEqual(checkpoints, ["cilium-images-cp1"])
        self.assertEqual([call.args[0] for call in host.call.call_args_list], ["component-images"])
        environment.client.evict_pod.assert_not_called()

    def test_unpinned_images_are_refused_before_cri_commands(self):
        with patch.object(component_images.common, "run") as command:
            for images in (["quay.io/cilium/cilium:v1.20.4"], [{}], ["--config=/private"], []):
                with self.assertRaises(OperationError):
                    component_images.prepare(dict(images=images), "a" * 64)
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
