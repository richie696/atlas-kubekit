# SPDX-License-Identifier: MIT
"""Bind HTTPS registry configuration to a public CA fingerprint and node facts."""
import base64
import copy
import os
from pathlib import Path
import re

from ..configuration import normalize,registry,revision
from .. import configuration_cluster
from ..environment import check_identity
from ..host_actions.common import digest
from ..models import OperationError
from ..registry_ca import public_ca


def certificate(path):
    if not path:return b''
    file=Path(path).expanduser().absolute()
    if (any(item.is_symlink() for item in (file,*file.parents)) or not file.is_file()
            or file.stat().st_uid!=os.geteuid() or file.stat().st_mode&0o022 or file.stat().st_size>64*1024):
        raise OperationError('ops.registry.certificate')
    return public_ca(file.read_bytes())


def from_arguments(args,configuration,environment):
    raw=certificate(args.ca_file)
    return dict(configuration=configuration,authority=args.registry,
                caFile=str(Path(args.ca_file).expanduser().absolute()) if args.ca_file else '',
                caFingerprint=digest(raw) if raw else '')


def preview(values):
    return (('ops.preview.registry',(values['authority'],values['caFingerprint'] or 'system')) ,)


def parameters(value):
    if not isinstance(value,dict) or set(value)!={'configuration','authority','caFile','caFingerprint'}:
        raise OperationError('ops.change.invalid_parameters')
    host=registry(value['authority'])
    if (not host or not isinstance(value['caFile'],str)
            or not isinstance(value['caFingerprint'],str) or not re.fullmatch(r'(?:[a-f0-9]{64})?',value['caFingerprint'])
            or bool(value['caFile'])!=bool(value['caFingerprint'])):
        raise OperationError('ops.change.invalid_parameters')
    return dict(configuration=normalize(value['configuration']),authority=host,
                caFile=value['caFile'],caFingerprint=value['caFingerprint'])


def desired(values):
    result=copy.deepcopy(values['configuration'])
    result['registry']={key:values[key] for key in ('authority','caFingerprint')}
    return normalize(result)


def observe(value,environment):
    values=parameters(value);config,_=environment.verify(values['configuration'])
    raw=certificate(values['caFile'])
    if (digest(raw) if raw else '')!=values['caFingerprint']:
        raise OperationError('ops.change.stale')
    saved=configuration_cluster.read(environment.client)
    if saved is not None and saved not in (config,desired(values)):
        raise OperationError('ops.config.conflict')
    rows={}
    for node in config['nodes']:
        if node['name'].startswith('lb'):continue
        row=environment.host(config,node['name']).call('registry-observe',
            dict(authority=values['authority'],certificate=base64.b64encode(raw).decode()))
        check_identity(config,node['name'],row['identity']);rows[node['name']]=row
    return dict(nodes=rows,sharedRevision=revision(saved) if saved else None)


def apply(value,environment,checkpoint,run_id):
    values=parameters(value);facts=observe(values,environment)
    raw=certificate(values['caFile']);result={}
    for name,row in facts['nodes'].items():
        checkpoint('registry-'+name)
        result[name]=environment.host(values['configuration'],name).call('registry-apply',dict(
            authority=values['authority'],certificate=base64.b64encode(raw).decode(),
            expected={key:row[key] for key in ('configurationSha','hostsSha')}),run_id)
    checkpoint('publish-configuration')
    configuration_cluster.publish(environment.client,desired(values),expected=facts['sharedRevision'])
    return dict(nodes=result,configurationRevision=revision(desired(values)),authenticatedPullVerified=False)
