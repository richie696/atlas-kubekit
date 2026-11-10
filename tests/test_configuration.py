"""Configuration rules, real private persistence and safe CLI/phase-two boundaries."""
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
import copy
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.operations import cli, collector, configuration as config, drift, profiles
from scripts.operations.locking import exclusive_lock
from scripts.operations.models import OperationError, exit_status
from scripts.lib import i18n

ROOT=Path(__file__).resolve().parents[1]
from tests.shell import BASH


def concurrent_import(values):
    state,data,old=values
    try:return profiles.import_profile(state,'test',data,old)[0]
    except OperationError as error:return error.key


def observed(data):
    rows={}
    for node in data['nodes']:
        lb=node['name'].startswith('lb')
        rows[node['name']]=dict(schemaVersion=1,hostname=node['name'],role='lb' if lb else 'cp',
            identityState='ok',identityFingerprint=node['identityFingerprint'],
            addresses=dict(state='ok',values=[dict(interface=node['interface'],address=node['address'])]),
            services=dict(haproxy='active',keepalived='active',containerd='active',kubelet='active'),
            versions=dict(data['versions']),network=dict(state='ok',values=dict(podSubnet=data['cluster']['podCIDR'],serviceSubnet=data['cluster']['serviceCIDR'])),
            deployment=dict(state='ok',values=dict(PROFILE=data['cluster']['profile'],LOCAL_REGISTRY_HOST='',SSH_USER=data['ssh']['user'])))
        if lb:
            rows[node['name']]['lb']=dict(state='ok',vips=[data['cluster']['apiVIP']],interface=node['interface'],
                vrrpID=data['cluster']['vrrpID'],apiPort=data['cluster']['apiPort'],statsPolicy='loopback',
                statsCIDRs=[],backends=[dict(name=n['name'],address=n['address'],port=data['cluster']['apiPort'])
                                        for n in data['nodes'] if n['name'].startswith('cp')])
    rows['lb1']['addresses']['values'].append(dict(interface='enp6s18',address=data['cluster']['apiVIP']))
    return rows


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None)
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name).resolve()


class ConfigurationRulesTests(TemporaryCase):
    def test_normalization_is_pure_and_revision_ignores_node_order(self):
        data=config.template();before=copy.deepcopy(data)
        normalized=config.normalize(data);normalized['cluster']['name']='different'
        self.assertEqual(data,before)
        data['nodes'].reverse();self.assertEqual(config.revision(data),config.revision(before))

    def test_reject_invalid_types_unknown_fields_and_unsafe_topologies(self):
        samples=[]
        for field,value in [('schemaVersion',True),('nodes',[]),('password','secret')]:
            data=config.template();data[field]=value;samples.append(data)
        for field,value in [('appVIP',False),('apiPort',True),('apiPort',7000),('apiVIP','127.0.0.1'),
            ('appVIP','192.0.2.11'),('apiVIP','192.0.2.11'),('podCIDR','10.96.0.0/12'),
            ('statsAllowedCIDR','0.0.0.0/0'),('localRegistryHost','$(touch /tmp/no):5000')]:
            data=config.template();data['cluster'][field]=value;samples.append(data)
        for field,value in [('name','lb1;id'),('interface','eth0\nreboot'),('address','10.244.0.1'),
                            ('dataDevice','/dev/../etc'),('dataDevice','/dev/sdb'),('identityFingerprint','abc')]:
            data=config.template();data['nodes'][0][field]=value;samples.append(data)
        data=config.template();data['ssh']['user']='root';samples.append(data)
        data=config.template();data['nodes'][1]['address']=data['nodes'][0]['address'];samples.append(data)
        for data in samples:
            with self.subTest(data=data),self.assertRaises(OperationError):config.normalize(data)

    def test_duplicate_json_and_symlinks_are_rejected_without_executing_input(self):
        source=self.base/'config.json';source.write_text('{"schemaVersion":1,"schemaVersion":1}')
        with self.assertRaises(OperationError):config.load_config(source)
        source.write_text(json.dumps(config.template()));link=self.base/'link';link.symlink_to(source)
        with self.assertRaises(OperationError):config.load_config(link)
        self.assertEqual(config.load_config(source),config.template())

    def test_bridge_uses_fixed_fields_and_refuses_unpinned_versions(self):
        data=config.template();rows=config.deployment_rows(data)
        self.assertIn(('VIP','192.0.2.10'),rows)
        self.assertEqual(rows[-1],('NODE','cp1','192.0.2.12','enp6s18','-','-'))
        data['versions']['kubernetes']='v1.37.1'
        self.assertEqual(config.normalize(data)['versions']['kubernetes'],'v1.37.1')
        with self.assertRaises(OperationError):config.deployment_rows(data)

    def test_identity_probe_and_collector_share_fingerprint_protocol(self):
        result=subprocess.run(['python3',str(ROOT/'scripts/operations/deployment_config.py'),'--identity-source'],capture_output=True,text=True,check=True)
        self.assertIn('def identity_facts():',result.stdout)
        compile(result.stdout,'identity-source','exec')

    def test_bridge_versions_match_installer_defaults(self):
        script='source "$1/versions.sh"; atlas_versions_load "$1"; printf "%s\\n%s\\n" "$ATLAS_KUBERNETES_VERSION" "$ATLAS_CILIUM_VERSION"'
        result=subprocess.run([BASH,'-c',script,'test',str(ROOT/'scripts/lib')],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.splitlines(),[config.PINNED_VERSIONS['kubernetes'],config.PINNED_VERSIONS['cilium']])


class ProfilePersistenceTests(TemporaryCase):
    def test_import_idempotency_revision_guard_and_private_roundtrip(self):
        data=config.template();state=self.base/'state'
        rev,changed=profiles.import_profile(state,'test',data)
        self.assertTrue(changed);file=state/'test.json';before=file.stat().st_mtime_ns
        self.assertEqual(profiles.import_profile(state,'test',data),(rev,False))
        self.assertEqual(file.stat().st_mtime_ns,before)
        changed_data=copy.deepcopy(data);changed_data['cluster']['apiPort']=7443
        with self.assertRaises(OperationError):profiles.import_profile(state,'test',changed_data)
        self.assertEqual(profiles.read_profile(state,'test'),(data,rev))
        next_rev,_=profiles.import_profile(state,'test',changed_data,rev)
        with self.assertRaises(OperationError):profiles.import_profile(state,'test',data,rev)
        self.assertNotEqual(rev,next_rev)
        self.assertEqual(file.stat().st_mode&0o777,0o600);self.assertEqual(state.stat().st_mode&0o777,0o700)

    def test_real_processes_cannot_both_overwrite_same_revision(self):
        state=self.base/'state';data=config.template();rev,_=profiles.import_profile(state,'test',data)
        a=copy.deepcopy(data);a['cluster']['apiPort']=7443
        b=copy.deepcopy(data);b['cluster']['apiPort']=8443
        with ProcessPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(concurrent_import,[(str(state),a,rev),(str(state),b,rev)]))
        self.assertEqual(sum(len(r)==64 for r in results),1)
        self.assertTrue(any(r in ('ops.config.conflict','ops.error.locked') for r in results))
        self.assertIn(profiles.read_profile(state,'test')[1],results)

    def test_locks_tampering_and_insecure_files_fail_closed(self):
        state=self.base/'state';data=config.template();profiles.import_profile(state,'test',data)
        with exclusive_lock(state/'locks','test'):
            with self.assertRaises(OperationError):profiles.import_profile(state,'test',data)
        file=state/'test.json';value=json.loads(file.read_text());value['config']['cluster']['apiPort']=7443
        file.write_text(json.dumps(value))
        with self.assertRaises(OperationError):profiles.read_profile(state,'test')
        file.chmod(0o644)
        with self.assertRaises(OperationError):profiles.read_profile(state,'test')

    def test_export_never_clobbers_existing_files_or_follows_symlinks(self):
        file=self.base/'out'/'cluster.json';data=config.template();profiles.export_config(file,data)
        self.assertEqual(config.load_config(file),data)
        with self.assertRaises(OperationError):profiles.export_config(file,data)
        link=self.base/'link';link.symlink_to(file)
        with self.assertRaises(OperationError):profiles.export_config(link,data)
        self.assertEqual(file.stat().st_mode&0o777,0o600)


class DriftAndCollectorTests(unittest.TestCase):
    def config(self):
        data=config.template()
        for i,node in enumerate(data['nodes']):node['identityFingerprint']=str(i+1)*64
        return data

    def test_complete_observations_pass_and_rules_do_not_mutate(self):
        data=self.config();facts=observed(data);before=copy.deepcopy(facts)
        rows=drift.findings(data,facts)
        self.assertEqual(exit_status(rows),0);self.assertEqual(before,facts)
        self.assertEqual(drift.plan(data,facts).operation,'configuration-review')

    def test_version_identity_network_and_exposed_stats_are_differences(self):
        data=self.config();facts=observed(data)
        facts['lb1']['identityFingerprint']='f'*64;facts['lb1']['lb']['statsPolicy']='unrestricted'
        facts['cp1']['versions']['kubernetes']='v1.35.0';facts['cp1']['network']['values']['podSubnet']='10.245.0.0/16'
        rows={r.check:r.status for r in drift.findings(data,facts)}
        for key in ('lb1.identity','lb1.stats-policy','cp1.kubelet-version','cp1.podCIDR'):self.assertEqual(rows[key],'FAIL')
        self.assertEqual(rows['cluster.apiVIP-owner'],'UNVERIFIED')
        facts['lb1']['lb']['statsPolicy']='unverified'
        self.assertEqual(next(r.status for r in drift.findings(data,facts) if r.check=='lb1.stats-policy'),'UNVERIFIED')

    def test_partial_lb_collection_never_proves_single_vip_owner(self):
        data=self.config();data['nodes'].append(dict(name='lb2',address='192.0.2.13',interface='enp6s18',dataDevice='',identityFingerprint='3'*64))
        facts=observed(data);facts['lb2']={}
        rows={r.check:r.status for r in drift.findings(data,facts)}
        self.assertEqual(rows['lb2.collection'],'UNVERIFIED');self.assertEqual(rows['cluster.apiVIP-owner'],'UNVERIFIED')

    def test_duplicate_vip_and_unknown_storage_never_pass(self):
        data=self.config();data['nodes'][1]['dataDevice']='existing'
        data['nodes'].append(dict(name='lb2',address='192.0.2.13',interface='enp6s18',dataDevice='',identityFingerprint='3'*64))
        facts=observed(data);facts['lb2']['addresses']['values'].append(dict(interface='enp6s18',address=data['cluster']['apiVIP']))
        rows={r.check:r.status for r in drift.findings(data,facts)}
        self.assertEqual(rows['cluster.apiVIP-owner-count'],'FAIL');self.assertEqual(rows['cp1.data-device'],'UNVERIFIED')

    def test_metadata_parsers_never_return_passwords_or_execute_shell(self):
        keep='''vrrp_instance VI_KUBE_APISERVER {
 interface enp6s18
 virtual_router_id 51
 authentication {
  auth_pass secret-password
 }
 virtual_ipaddress {
  192.0.2.10/32
 }
}
'''
        hap='''frontend kube-apiserver
 bind *:6443
 default_backend kube-apiserver-backend
backend kube-apiserver-backend
 server cp1 192.0.2.12:6443 check
frontend stats
 bind *:7000
 acl stats_source_allowed src 192.0.2.0/24
 http-request deny if !stats_source_allowed
'''
        value=collector.lb_configuration(keep,hap)
        self.assertEqual(value['statsPolicy'],'cidr');self.assertNotIn('secret-password',json.dumps(value))
        self.assertEqual(value['vips'],['192.0.2.10'])
        with self.assertRaises(ValueError):collector.lb_configuration(keep+keep,hap)
        with self.assertRaises(ValueError):collector.lb_configuration(keep,hap.replace('default_backend kube-apiserver-backend','default_backend other'))
        with self.assertRaises(ValueError):collector.lb_configuration(keep,hap.replace('frontend stats',' server unexpected 192.0.2.20:6443 check\nfrontend stats'))
        self.assertEqual(collector.lb_configuration(keep,hap+' bind 0.0.0.0:7000 ssl crt /tmp/cert\n')['statsPolicy'],'unverified')
        parsed=collector.deployment_values("SSH_USER=ubuntu\nAUTH_PASS=secret\nPROFILE=standard\nLOCAL_REGISTRY_HOST=''\n")
        self.assertEqual(parsed['values']['LOCAL_REGISTRY_HOST'],'');self.assertNotIn('AUTH_PASS',parsed['values'])


class ConfigurationEntryTests(TemporaryCase):
    def call(self,args):
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            return cli.main(['--output-dir',str(self.base/'reports'),'--state-dir',str(self.base/'profiles'),*args])

    def test_template_validate_import_export_never_collect_nodes(self):
        source=self.base/'template.json'
        with patch('scripts.operations.configuration_cli.collect_nodes') as nodes,patch('scripts.operations.commands.inspection.node_facts') as facts:
            self.assertEqual(self.call(['--config-template','--export-file',str(source)]),0)
            self.assertEqual(self.call(['--config-check','--config',str(source)]),0)
            self.assertEqual(self.call(['--config-import','--config',str(source)]),0)
            self.assertEqual(self.call(['--config-export','--profile-name','example-cluster','--export-file',str(self.base/'export.json')]),0)
            nodes.assert_not_called();facts.assert_not_called()
        self.assertEqual(config.load_config(source),config.load_config(self.base/'export.json'))
        reports=list((self.base/'reports').glob('*/report.json'));self.assertEqual(len(reports),4)
        self.assertEqual(len({json.loads(p.read_text())['runId'] for p in reports}),4)

    def test_conflict_writes_failure_report_and_drift_is_not_executed(self):
        source=self.base/'cluster.json';data=config.template();source.write_text(json.dumps(data))
        self.assertEqual(self.call(['--config-import','--config',str(source)]),0)
        data['cluster']['apiPort']=7443;source.write_text(json.dumps(data))
        self.assertEqual(self.call(['--config-import','--config',str(source)]),1)
        self.assertTrue(any(json.loads(p.read_text())['result']=='failed' for p in (self.base/'reports').glob('*/report.json')))
        with patch('scripts.operations.configuration_cli.collect_nodes',return_value={}):
            self.assertEqual(self.call(['--config-drift','--config',str(source)]),2)
        reports=[json.loads(p.read_text()) for p in (self.base/'reports').glob('*/report.json')]
        self.assertEqual(next(p['planExecution'] for p in reports if p['operation']=='config-drift'),'not-executed')

    def test_phase_two_validation_exits_before_any_mutation_or_ssh(self):
        source=self.base/'cluster.json';source.write_text(json.dumps(config.template()))
        result=subprocess.run([BASH,str(ROOT/'02-deploy-cluster.sh'),'--lang','zh_CN','--config-check',str(source)],
                              cwd=self.base,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('未连接节点',result.stdout)
        for args in (['--config-check'],['--config-check',''],['--config',''],
                     ['--config-check',str(source),'--config',str(source)]):
            rejected=subprocess.run([BASH,str(ROOT/'02-deploy-cluster.sh'),'--lang','zh_CN',*args],capture_output=True,text=True)
            self.assertEqual(rejected.returncode,1,rejected.stderr)
            self.assertIn('配置或参数组合无效',rejected.stderr)
        data=config.template();data['versions']['cilium']='1.21.0';source.write_text(json.dumps(data))
        result=subprocess.run([BASH,str(ROOT/'02-deploy-cluster.sh'),'--lang','en','--config-check',str(source)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('pinned',result.stderr)

    def test_phase_two_saved_plan_guard_uses_actual_shell_function(self):
        source=(ROOT/'scripts/deploy/configuration.sh').read_text()
        function=re.search(r'^atlas_deploy_validate_plan\(\) \{\n.*?^\}',source,re.M|re.S).group()
        prefix=['set -euo pipefail','die() { exit 17; }','atlas_t() { printf %s "$1"; }',
                'declare -A IP NODE_IFACE DISK CONFIG_VALUES CONFIG_IP CONFIG_IFACE CONFIG_DISK',
                'NODES=(lb1 cp1); CONFIG_NODES=(lb1 cp1)',function]
        for row in config.deployment_rows(config.template()):
            if row[0]=='NODE':
                _,name,address,interface,disk,pin=row
                for array,value in [('IP',address),('CONFIG_IP',address),('NODE_IFACE',interface),('CONFIG_IFACE',interface),('CONFIG_DISK',disk)]:
                    prefix.append(array+'['+name+']='+shlex.quote(value))
            else:
                key,value=row;prefix+=[key+'='+shlex.quote(value),'CONFIG_VALUES['+key+']='+shlex.quote(value)]
        for change,expected in [('',0),('VIP=192.0.2.20',17),('IP[cp1]=192.0.2.99',17),
                                ('NODE_IFACE[cp1]=eth9',17),('CONFIG_DISK[cp1]=/dev/sdb',17),
                                ('NODES=(lb1 cp1 w1)',17)]:
            result=subprocess.run([BASH,'-c','\n'.join(prefix+[change,'atlas_deploy_validate_plan'])],capture_output=True,text=True)
            self.assertEqual(result.returncode,expected,(change,result.stderr))

    def test_phase_two_pins_persist_atomically_for_resume_and_are_idempotent(self):
        source=(ROOT/'scripts/deploy/configuration.sh').read_text()
        function=re.search(r'^atlas_deploy_persist_identity\(\) \{\n.*?^\}',source,re.M|re.S).group()
        file=self.base/'cluster.conf';file.write_text('VIP=192.0.2.10\n');file.chmod(0o600)
        program='print("fixed identity probe")\n'
        prefix=['set -euo pipefail','die() { exit 17; }','atlas_t() { printf %s "$1"; }',
            'declare -A NODE_IDENTITY_PIN; NODES=(lb1 cp1)',
            'STATE='+shlex.quote(str(self.base)),'CONF='+shlex.quote(str(file)),
            'NODE_IDENTITY_SOURCE='+shlex.quote(program),'NODE_IDENTITY_PIN[lb1]='+('a'*64),function]
        result=subprocess.run([BASH,'-c','\n'.join(prefix+['atlas_deploy_persist_identity'])],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        before=file.read_bytes();mtime=file.stat().st_mtime_ns
        result=subprocess.run([BASH,'-c','\n'.join(prefix+['atlas_deploy_persist_identity','NODE_IDENTITY_SOURCE=""; unset NODE_IDENTITY_PIN[lb1]',
            '. "$CONF"','printf "%s" "${NODE_IDENTITY_PIN[lb1]}"'])],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,'a'*64)
        self.assertEqual(file.stat().st_mtime_ns,mtime);self.assertEqual(file.read_bytes(),before)
        self.assertEqual(file.stat().st_mode&0o777,0o600)
        result=subprocess.run([BASH,'-c','\n'.join(prefix+['sed() { return 1; }','atlas_deploy_persist_identity'])],capture_output=True,text=True)
        self.assertEqual(result.returncode,17);self.assertEqual(file.read_bytes(),before)
        self.assertEqual(list(self.base.glob('.config-identity.*')),[])

    def test_all_new_localized_help_and_rejection_paths(self):
        for language in ('en','zh_CN','zh_TW'):
            with redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as done:cli.main(['--lang',language,'--help'])
            self.assertEqual(done.exception.code,0);self.assertIn('--config-drift',output.getvalue())
            self.assertEqual(self.call(['--lang',language,'--config-check','--config',str(self.base/'absent')]),1)

    def test_archive_metadata_is_ignored_but_invalid_visible_catalogs_fail(self):
        catalogs=self.base/'locales';catalogs.mkdir()
        for name in ('en','zh_cn','zh_tw'):shutil.copyfile(ROOT/'locales'/(name+'.json'),catalogs/(name+'.json'))
        (catalogs/'._en.json').write_bytes(b'\x00\x05\xa9binary metadata')
        with patch.object(i18n,'LOCALES_DIR',catalogs):
            self.assertEqual(set(i18n.available_locales()),{'en','zh_CN','zh_TW'})
            self.assertEqual(i18n.validate_catalogs(),[])
            self.assertEqual(self.call(['--config-template','--export-file',str(self.base/'template.json')]),0)
            (catalogs/'bad.json').write_bytes(b'\xa9invalid utf8')
            with self.assertRaises(i18n.LocalizationError):i18n.available_locales()


if __name__=='__main__':unittest.main()
