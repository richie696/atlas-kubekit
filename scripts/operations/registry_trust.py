# SPDX-License-Identifier: MIT
"""Public CA distribution contract consumed by worker lifecycle operations."""
import base64

from .environment import check_identity
from .host_actions.common import digest
from .models import OperationError


def installed(configuration,environment):
    entry=configuration.get('registry')
    if entry is None:return None
    result=environment.host(configuration,'cp1').call('registry-certificate',entry)
    check_identity(configuration,'cp1',result['identity'])
    try:
        raw=base64.b64decode(result['certificate'],validate=True)
    except (ValueError,TypeError):
        raise OperationError('ops.registry.certificate') from None
    if (digest(raw) if raw else '')!=entry['caFingerprint']:
        raise OperationError('ops.error.integrity')
    return dict(authority=entry['authority'],certificate=result['certificate'])


def probe(configuration,environment,name,request):
    if request is None:return None
    result=environment.host(configuration,name).call('registry-probe',request)
    check_identity(configuration,name,result['identity'])
    return dict(httpsStatus=result['httpsStatus'])


def apply(configuration,environment,name,request,run_id):
    if request is None:return None
    host=environment.host(configuration,name)
    facts=host.call('registry-observe',request)
    check_identity(configuration,name,facts['identity'])
    return host.call('registry-apply',dict(request,expected={key:facts[key] for key in ('configurationSha','hostsSha')}),run_id)
