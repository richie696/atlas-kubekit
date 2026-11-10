# SPDX-License-Identifier: MIT
"""Per-registry HTTPS trust without changing system-wide CA or disabling TLS."""
import base64
import json
import re
from pathlib import Path
import ssl
import urllib.error
import urllib.request

from ..configuration import registry as authority
from ..collector import identity_facts
from ..models import OperationError
from ..registry_ca import public_ca
from . import common

DIRECTORY=Path('/etc/containerd/certs.d')
CONFIG=Path('/etc/containerd/config.toml')
MARKER='# Managed by Atlas KubeKit: HTTPS registry trust.'


def namespace(host):
    return DIRECTORY/(host.replace(':','_')+'_')


def values(value):
    if not isinstance(value,dict) or set(value)-{'authority','certificate','expected'} or not {'authority','certificate'}<=set(value):
        raise OperationError('ops.change.invalid_parameters')
    host=authority(value['authority'])
    if not host:
        raise OperationError('ops.change.invalid_parameters')
    try:
        raw=base64.b64decode(value['certificate'],validate=True)
        public_ca(raw)
    except (ValueError,TypeError,ssl.SSLError,UnicodeError):
        raise OperationError('ops.registry.certificate') from None
    return host,raw


def endpoint(host,certificate):
    try:
        context=ssl.create_default_context(cadata=certificate.decode('ascii')) if certificate else ssl.create_default_context()
        # No proxy credentials or cross-authority redirect may reach another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),NoRedirect())
        try:
            with opener.open('https://'+host+'/v2/',timeout=8) as response:status=response.status
        except urllib.error.HTTPError as response:
            status=response.code
        if status not in (200,401):raise ValueError()
        return status
    except (OSError,ValueError,ssl.SSLError):
        raise OperationError('ops.registry.endpoint') from None


def observe(value):
    host,certificate=values(value)
    config=common.read_owned(CONFIG)
    _,dump=common.run(['containerd','config','dump'])
    if not re.search(r'''(?m)^\s*config_path\s*=\s*['"]/etc/containerd/certs\.d['"]\s*$''',dump):
        raise OperationError('ops.host.layout')
    file=namespace(host)/'hosts.toml'
    for candidate in (file,DIRECTORY/host/'hosts.toml'):
        if candidate.exists():
            content=common.read_owned(candidate)
            if MARKER.encode() not in content and b'Managed by Atlas KubeKit: local HTTP image registry.' not in content:
                raise OperationError('ops.host.layout')
    old=common.read_owned(file) if file.exists() else None
    if old is not None and MARKER.encode() not in old and b'Managed by Atlas KubeKit: local HTTP image registry.' not in old:
        raise OperationError('ops.host.layout')
    active,_=common.run(['systemctl','is-active','containerd'],check=False)
    if active!=0:raise OperationError('ops.host.inactive')
    return dict(identity=identity_facts(),configurationSha=common.digest(config),
        hostsSha=common.digest(old) if old is not None else None,httpsStatus=endpoint(host,certificate))


def apply(value,run_id):
    host,certificate=values(value)
    facts=observe(value)
    if value.get('expected')!={key:facts[key] for key in ('configurationSha','hostsSha')}:
        raise OperationError('ops.change.stale')
    directory=namespace(host)
    if any(path.is_symlink() for path in (directory,*directory.parents)):
        raise OperationError('ops.error.path')
    directory.mkdir(mode=0o755,parents=True,exist_ok=True)
    if directory.stat().st_uid!=0 or directory.stat().st_mode&0o022:
        raise OperationError('ops.error.path')
    file=directory/'hosts.toml';old=common.read_owned(file) if file.exists() else None
    lines=[MARKER,'server = "https://'+host+'"','', '[host."https://'+host+'"]',
           '  capabilities = ["pull", "resolve"]']
    if certificate:
        ca=directory/('atlas-ca-'+common.digest(certificate)+'.crt')
        if ca.exists() and common.read_owned(ca)!=certificate:raise OperationError('ops.change.stale')
        common.replace(ca,certificate,0o644)
        lines.append('  ca = "'+str(ca)+'"')
    desired=('\n'.join(lines)+'\n').encode()
    journal=common.journal(run_id,'registry-configure')
    if old is not None:common.replace(journal/'hosts.before',old)
    if common.digest(common.read_owned(CONFIG))!=facts['configurationSha']:
        raise OperationError('ops.change.stale')
    current=common.read_owned(file) if file.exists() else None
    if current!=old:raise OperationError('ops.change.stale')
    try:
        common.replace(file,desired,0o644)
        endpoint(host,certificate)
        common.record(journal,'registry-configure','succeeded',authority=host)
    except BaseException:
        if old is None:file.unlink(missing_ok=True)
        else:common.replace(file,old,0o644)
        common.record(journal,'registry-configure','rolled-back')
        raise
    return dict(authority=host,changed=current!=desired,transportVerified=True,
                authenticatedPullVerified=False,restartRequired=False)


def probe(value):
    host,raw=values(value)
    return dict(identity=identity_facts(),httpsStatus=endpoint(host,raw))


def certificate(value):
    if not isinstance(value,dict) or set(value)!={'authority','caFingerprint'}:
        raise OperationError('ops.change.invalid_parameters')
    host=authority(value['authority']);fingerprint=value['caFingerprint']
    import re
    if not host or not isinstance(fingerprint,str) or not re.fullmatch(r'(?:[a-f0-9]{64})?',fingerprint):
        raise OperationError('ops.change.invalid_parameters')
    raw=common.read_owned(namespace(host)/('atlas-ca-'+fingerprint+'.crt'),limit=64*1024) if fingerprint else b''
    if raw and common.digest(raw)!=fingerprint:
        raise OperationError('ops.error.integrity')
    public_ca(raw)
    return dict(certificate=base64.b64encode(raw).decode(),identity=identity_facts())
