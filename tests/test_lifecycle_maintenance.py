"""Maintenance rule boundaries; native PDB/eviction behavior needs a real API."""
import copy
import unittest

from scripts.operations.configuration import template,normalize
from scripts.operations.lifecycle import maintenance,rules
from scripts.operations.models import OperationError


def node(name,control=False,ready=True):
    return dict(metadata=dict(name=name,uid='uid-'+name,labels={'node-role.kubernetes.io/control-plane':''} if control else {}),
                spec=dict(),status=dict(conditions=[dict(type='Ready',status='True' if ready else 'False')],nodeInfo=dict(kubeletVersion='v1.36.5')))


class Client:
    def __init__(self,nodes):self.nodes=nodes;self.calls=[];self.pods=[];self.pdbs=[];self.pv={}
    def get(self,kind,name=None,namespace=None):
        if kind=='node':return copy.deepcopy(self.nodes[name]) if name else dict(items=list(self.nodes.values()))
        if kind=='pdb':return dict(items=self.pdbs)
        if kind=='persistentvolumeclaim':return dict(spec=dict(volumeName='pv1'))
        if kind=='persistentvolume':return self.pv
    def node_pods(self,name):return copy.deepcopy(self.pods)
    def call(self,args,**kwargs):
        self.calls.append(args)
        return [dict(health=True)] * 3 if 'etcdctl' in args else ''
    def replace(self,value):self.nodes[value['metadata']['name']]=copy.deepcopy(value);return value


class Environment:
    def __init__(self,nodes):self.client=Client(nodes)
    def verify(self,config):return config,self.client.nodes


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        config=template();config['nodes'].extend(dict(name=n,address='192.0.2.'+str(i),interface='eth0') for i,n in enumerate(('cp2','cp3','w1'),20))
        self.config=normalize(config);self.values=dict(configuration=self.config,node='w1',action='drain',deleteEmptyDir=False)
        self.environment=Environment({n:node(n,n.startswith('cp')) for n in ('cp1','cp2','cp3','w1')})

    def pod(self,volume=None,owner='ReplicaSet'):
        return dict(metadata=dict(name='app',namespace='app',uid='pod-uid',ownerReferences=[dict(kind=owner,uid='controller')]),
                    spec=dict(volumes=[volume] if volume else []),status=dict(phase='Running'))

    def test_drain_plan_is_read_only_and_uses_server_dry_run(self):
        before=copy.deepcopy(self.environment.client.nodes)
        maintenance.observe(self.values,self.environment)
        self.assertEqual(before,self.environment.client.nodes)
        self.assertIn('--dry-run=server',self.environment.client.calls[-1])

    def test_drain_cordons_and_uses_native_eviction_without_force(self):
        checkpoints=[];maintenance.apply(self.values,self.environment,checkpoints.append)
        self.assertTrue(self.environment.client.nodes['w1']['spec']['unschedulable'])
        self.assertNotIn('--force',self.environment.client.calls[-1]);self.assertNotIn('--dry-run=server',self.environment.client.calls[-1])
        self.assertEqual(checkpoints,['scheduling-drain-w1','evict-workloads-w1'])

    def test_local_data_and_local_persistent_volumes_block_drain(self):
        for volume in (dict(hostPath=dict(path='/data')),dict(emptyDir={})):
            self.environment.client.pods=[self.pod(volume)]
            with self.assertRaises(OperationError):maintenance.observe(self.values,self.environment)
        self.environment.client.pods=[self.pod(dict(persistentVolumeClaim=dict(claimName='claim')))]
        self.environment.client.pv=dict(spec=dict(local=dict(path='/data')))
        with self.assertRaises(OperationError):maintenance.observe(self.values,self.environment)

    def test_emptydir_requires_explicit_intent_and_daemonsets_remain(self):
        values=copy.deepcopy(self.values);values['deleteEmptyDir']=True
        self.environment.client.pods=[self.pod(dict(emptyDir={}))]
        maintenance.observe(values,self.environment)
        self.assertIn('--delete-emptydir-data',self.environment.client.calls[-1])
        pods=[self.pod(dict(hostPath=dict(path='/var/lib')),owner='DaemonSet')]
        self.assertEqual(rules.movable_pods(pods,False),[])

    def test_control_plane_maintenance_requires_other_healthy_majority(self):
        values=copy.deepcopy(self.values);values['node']='cp1'
        self.environment.client.nodes['cp2']=node('cp2',True,False)
        with self.assertRaises(OperationError):maintenance.observe(values,self.environment)
        self.environment.client.nodes['cp2']=node('cp2',True,True)
        maintenance.observe(values,self.environment)

    def test_unready_node_cannot_return_to_scheduling(self):
        values=copy.deepcopy(self.values);values['action']='uncordon'
        self.environment.client.nodes['w1']=node('w1',False,False)
        with self.assertRaises(OperationError):maintenance.observe(values,self.environment)

    def test_zero_or_stale_pdb_budget_blocks_before_any_cordon(self):
        self.environment.client.pods=[self.pod()]
        self.environment.client.pods[0]['metadata']['labels']={'app':'protected'}
        budget=dict(metadata=dict(name='protect',namespace='app',uid='budget',generation=2),
                    spec=dict(selector=dict(matchLabels={'app':'protected'})),
                    status=dict(disruptionsAllowed=0,observedGeneration=2))
        self.environment.client.pdbs=[budget]
        for allowed,generation in ((0,2),(1,1)):
            budget['status'].update(disruptionsAllowed=allowed,observedGeneration=generation)
            with self.assertRaises(OperationError):maintenance.observe(self.values,self.environment)
            self.assertNotIn('unschedulable',self.environment.client.nodes['w1']['spec'])
        budget['status'].update(disruptionsAllowed=1,observedGeneration=2)
        maintenance.observe(self.values,self.environment)

    def test_pdb_set_selectors_and_v1_empty_selector(self):
        self.assertTrue(rules.selector_matches({},{}))
        self.assertFalse(rules.selector_matches(None,{}))
        self.assertTrue(rules.selector_matches(dict(matchExpressions=[dict(key='zone',operator='NotIn',values=['a'])]),{}))
        self.assertFalse(rules.selector_matches(dict(matchExpressions=[dict(key='zone',operator='Exists')]),{}))

    def test_local_pvc_on_ignored_daemonset_does_not_block_eviction(self):
        self.environment.client.pods=[self.pod(dict(persistentVolumeClaim=dict(claimName='claim')),owner='DaemonSet')]
        self.environment.client.pv=dict(spec=dict(local=dict(path='/data')))
        maintenance.observe(self.values,self.environment)


if __name__=='__main__':unittest.main()
