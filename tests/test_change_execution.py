"""Pure execution contracts and CAS state handling; real API evidence is separate."""
import copy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import threading
import unittest

from scripts.operations.changes import plans,store,execution
from scripts.operations.changes.coordinator import ClusterCoordinator,managed_map
from scripts.operations.models import OperationError


class Client:
    def __init__(self):self.maps={};self.serial=0;self.lock=threading.Lock()
    def cluster_identity(self):return 'cluster-12345678'
    def get(self,kind,name,namespace=None,optional=False):
        with self.lock:return copy.deepcopy(self.maps.get((namespace,name)))
    def create(self,value):
        with self.lock:
            key=(value['metadata']['namespace'],value['metadata']['name'])
            if key in self.maps:return None
            self.serial+=1;value=copy.deepcopy(value);value['metadata']['resourceVersion']=str(self.serial)
            self.maps[key]=value;return copy.deepcopy(value)
    def replace(self,value):
        with self.lock:
            key=(value['metadata']['namespace'],value['metadata']['name']);old=self.maps.get(key)
            if old is None or old['metadata']['resourceVersion']!=value['metadata']['resourceVersion']:return None
            self.serial+=1;value=copy.deepcopy(value);value['metadata']['resourceVersion']=str(self.serial)
            self.maps[key]=value;return copy.deepcopy(value)


class PlanTests(unittest.TestCase):
    def plan(self,**kwargs):return plans.make('node-maintenance','cluster-12345678',{'ready':True},{'node':'w1','action':'cordon'},**kwargs)
    def test_identity_changes_with_intent_and_parameters_have_no_shared_mutation(self):
        plan=self.plan();values=plan.parameters;values['node']='w2'
        self.assertEqual(plan.parameters['node'],'w1')
        self.assertEqual(plans.load(plan.record()),plan)
        with self.assertRaises(Exception):plan.id='changed'

    def test_tamper_expiry_unknown_capability_and_credentials_are_rejected(self):
        for field,value in [('id','a'*64),('capability','shell'),('capability',[]),('clusterIdentity','other')]:
            body=self.plan().record();body[field]=value
            with self.subTest(field=field),self.assertRaises(OperationError):plans.load(body)
        now=datetime.now(timezone.utc);plan=self.plan(now=now-timedelta(hours=1))
        with self.assertRaises(OperationError):plans.load(plan.record(),now)
        for key in ('token','password','script','command','privateKey'):
            with self.assertRaises(OperationError):plans.make('node-maintenance','cluster-12345678',{}, {key:'secret'})

    def test_plan_files_are_private_and_never_replace_existing(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None) as directory:
            file=Path(directory).resolve()/'plan.json';plan=self.plan();store.save(file,plan)
            self.assertEqual(store.read(file),plan);self.assertEqual(file.stat().st_mode&0o777,0o600)
            with self.assertRaises(OperationError):store.save(file,plan)
            file.chmod(0o644)
            with self.assertRaises(OperationError):store.read(file)


class CoordinationTests(unittest.TestCase):
    def setUp(self):self.client=Client();self.coordinator=ClusterCoordinator(self.client,'kube-system')
    def test_one_owner_and_safe_release(self):
        with self.coordinator.hold('a'*64):
            with self.assertRaises(OperationError):
                with ClusterCoordinator(self.client,'kube-system').hold('b'*64):pass
            self.coordinator.assert_owner()
        self.assertEqual(self.coordinator.status()['data'],{})

    def test_foreign_object_and_stolen_ownership_are_not_overwritten(self):
        value=managed_map(self.coordinator.name,'kube-system',{});value['metadata']['labels']={}
        self.client.create(value)
        with self.assertRaises(OperationError):
            with self.coordinator.hold('a'*64):pass
        self.client.maps.clear()
        with self.coordinator.hold('a'*64):
            value=self.coordinator.status();value['data']['owner']='different';self.client.replace(value)
            with self.assertRaises(OperationError):self.coordinator.assert_owner()
        self.assertEqual(self.coordinator.status()['data']['owner'],'different')

    def test_retained_lock_requires_exact_owner_version_and_explicit_confirmation(self):
        with self.coordinator.hold('a'*64):self.coordinator.retain=True
        value=self.coordinator.status();owner=value['data']['owner'];version=value['metadata']['resourceVersion']
        for args in [(owner,version,'yes'),('other',version,'OLD COORDINATOR AND REMOTE JOBS STOPPED'),
                     (owner,'old','OLD COORDINATOR AND REMOTE JOBS STOPPED')]:
            with self.assertRaises(OperationError):self.coordinator.recover(*args)
        self.coordinator.recover(owner,version,'OLD COORDINATOR AND REMOTE JOBS STOPPED')
        self.assertEqual(self.coordinator.status()['data'],{})

    def test_success_is_cross_coordinator_idempotent(self):
        observation={'ready':True};plan=plans.make('node-maintenance','cluster-12345678',observation,{'node':'w1','action':'cordon'})
        effects=[]
        def apply(params,checkpoint):checkpoint('cordon');effects.append(params['node']);return {'done':True}
        result=execution.execute(plan.record(),plan.id,self.coordinator,lambda p:observation,apply,lambda p:p)
        self.assertEqual(result['state'],'succeeded')
        other=ClusterCoordinator(self.client,'kube-system')
        result=execution.execute(plan.record(),plan.id,other,lambda p:observation,apply,lambda p:p)
        self.assertEqual(result['state'],'already-succeeded');self.assertEqual(effects,['w1'])

    def test_confirmation_stale_observations_and_wrong_cluster_have_no_effect(self):
        plan=plans.make('node-maintenance','cluster-12345678',{'ready':True},{'node':'w1','action':'cordon'})
        effects=[]
        for confirm,observed in [('yes',{'ready':True}),(plan.id,{'ready':False})]:
            with self.assertRaises(OperationError):execution.execute(plan.record(),confirm,self.coordinator,lambda p:observed,lambda p,c:effects.append(p),lambda p:p)
        wrong=plans.make('node-maintenance','another-cluster',{}, {})
        with self.assertRaises(OperationError):execution.execute(wrong.record(),wrong.id,self.coordinator,lambda p:{},lambda p,c:effects.append(p),lambda p:p)
        self.assertEqual(effects,[])

    def test_partial_failure_records_error_and_retains_cluster_lock(self):
        plan=plans.make('node-maintenance','cluster-12345678',{}, {})
        def fail(params,checkpoint):checkpoint('drain');raise OperationError('ops.change.failed')
        with self.assertRaises(OperationError):execution.execute(plan.record(),plan.id,self.coordinator,lambda p:{},fail,lambda p:p)
        self.assertEqual(self.coordinator.journal(plan.id)['state'],'failed')
        self.assertTrue(self.coordinator.status()['data']['owner'])


if __name__=='__main__':unittest.main()
