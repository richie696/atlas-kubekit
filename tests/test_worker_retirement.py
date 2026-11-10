"""Worker withdrawal preserves disks and stops only declared daemon sandboxes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from scripts.operations.configuration import template,normalize
from scripts.operations.host_actions import retirement,common
from scripts.operations.lifecycle import retire,replace,retirement_storage
from scripts.operations.models import OperationError


class RetirementTests(unittest.TestCase):
    def config(self):
        value=template();value['nodes'].append(dict(name='w1',address='192.0.2.20',interface='eth0',dataDevice='none'))
        return normalize(value)

    def test_control_planes_and_duplicate_replacement_identity_are_rejected(self):
        values=dict(configuration=self.config(),node='cp1',nodeUID='old-node-uid',deleteEmptyDir=False)
        with self.assertRaises(OperationError):retire.parameters(values)
        values['node']='w1';values['candidate']=dict(name='w2',address='192.0.2.20',interface='eth0',dataDevice='none')
        with self.assertRaises(OperationError):replace.parameters(values)

    def test_local_volume_without_proven_other_node_affinity_blocks_retirement(self):
        client=Mock();node=dict(metadata=dict(name='w1',labels={'kubernetes.io/hostname':'w1'}))
        pv=dict(metadata=dict(uid='pv-uid',name='pv'),spec=dict(local=dict(path='/data')))
        client.get.return_value=dict(items=[pv]);client.call.return_value=''
        with self.assertRaises(OperationError):retirement_storage.require_evacuated(client,node)
        pv['spec']['nodeAffinity']=dict(required=dict(nodeSelectorTerms=[dict(matchExpressions=[dict(
            key='kubernetes.io/hostname',operator='In',values=['w2'])])]))
        self.assertEqual(len(retirement_storage.require_evacuated(client,node)['localVolumesOnOtherNodes']),1)
        client.call.return_value='replicas.longhorn.io\n'
        client.get.side_effect=[dict(items=[]),dict(items=[dict(spec=dict(nodeID='w1'))])]
        with self.assertRaises(OperationError):retirement_storage.require_evacuated(client,node)

    def test_only_expected_daemon_pods_are_stopped_and_disks_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();marker=root/'retired.json';calls=[]
            values=dict(configuration=self.config(),node='w1',nodeUID='old-node-uid',
                        clusterIdentity='cluster-12345678',podUIDs=['daemon-pod-uid'])
            def run(args,**kwargs):
                calls.append(args)
                if 'pods' in args:return 0,json.dumps(dict(items=[dict(id='a'*64,state='SANDBOX_READY',labels={'io.kubernetes.pod.uid':'daemon-pod-uid'})]))
                return (3,'inactive') if 'is-active' in args else (0,'')
            with patch.object(retirement,'MARKER',marker),patch.object(common,'JOURNAL_DIRECTORY',root/'journal'),\
                    patch.object(retirement,'observe',return_value=dict(retirement=None)),patch.object(common,'run',run):
                result=retirement.apply(values,'a'*64)
            self.assertTrue(result['disksPreserved'])
            self.assertEqual(json.loads(marker.read_text())['state'],'retired')
            self.assertTrue(any('stopp' in args and 'a'*64 in args for args in calls))
            self.assertFalse(any(any(word in args for word in ('kubeadm','mkfs','rm')) for args in calls))


if __name__=='__main__':unittest.main()
