# SPDX-License-Identifier: MIT
"""Render and safely publish one Alertmanager Operator email route."""
import copy
import re

from ..adapters.kubernetes import resource_name
from ..models import OperationError, fingerprint
from . import alert_routing, email_templates, publication
from .channels import email
from .rules import _DNS_LABEL, _dns_name

ALERTMANAGER = "alertmanagers.monitoring.coreos.com"
CONFIG = "alertmanagerconfigs.monitoring.coreos.com"


def manifest(namespace, release, version, host, port, sender, recipient, username, secret, key, severity, lang,
             route_policy=None):
    if (not isinstance(namespace, str) or not _DNS_LABEL.fullmatch(namespace) or
            not isinstance(release, str) or len(release) > 53 or not _dns_name(release) or
            not isinstance(secret, str) or not _dns_name(secret) or
            not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,253}', key) or
            severity not in ('warning', 'critical', 'info')):
        raise ValueError('invalid email route')
    native = email.receiver('atlas-email', version, host, port, sender, recipient, username, '/secret/password')
    native_config = native['email_configs'][0]
    template = email_templates.templates(lang)
    routing = alert_routing.route_policy() if route_policy is None else alert_routing.route_policy(
        route_policy.get('matchers'), route_policy.get('groupBy'), route_policy.get('groupWait'),
        route_policy.get('groupInterval'), route_policy.get('repeatInterval'))
    config = {'to': native_config['to'], 'from': native_config['from'],
              'smarthost': native_config['smarthost'], 'authUsername': native_config['auth_username'],
              'authPassword': {'name': secret, 'key': key}, 'sendResolved': True,
              'requireTLS': native_config['require_tls'], 'headers': [{'key': 'Subject', 'value': template['subject']}],
              'html': template['html']}
    return {'apiVersion': 'monitoring.coreos.com/v1alpha1', 'kind': 'AlertmanagerConfig',
            'metadata': {'name': 'atlas-email', 'namespace': namespace,
                         'labels': {'release': release, publication.OWNER: 'monitoring'}},
            'spec': {'route': {'receiver': 'atlas-email', 'continue': True,
                               **alert_routing.render_route(severity, routing)},
                     'receivers': [{'name': 'atlas-email', 'emailConfigs': [config]}]}}


def capture(client, desired, manager_name, version, secret, key):
    namespace = resource_name(desired['metadata']['namespace'])
    manager = client.get(ALERTMANAGER, resource_name(manager_name), namespace)
    meta = publication._metadata(manager)
    spec = manager.get('spec', {})
    if not isinstance(spec, dict):
        raise OperationError('ops.monitoring.email_incompatible')
    selector = spec.get('alertmanagerConfigSelector')
    namespaces = spec.get('alertmanagerConfigNamespaceSelector')
    strategy_config = spec.get('alertmanagerConfigMatcherStrategy') or {}
    if not isinstance(strategy_config, dict):
        raise OperationError('ops.monitoring.email_incompatible')
    strategy = strategy_config.get('type', 'OnNamespace')
    image = spec.get('image')
    if (not isinstance(selector, dict) or set(selector) - {'matchLabels'} or
            not isinstance(selector.get('matchLabels', {}), dict) or
            any(desired['metadata']['labels'].get(k) != v for k, v in selector.get('matchLabels', {}).items()) or
            namespaces not in (None, {}) or strategy not in ('None', 'OnNamespaceExceptForAlertmanagerNamespace') or
            not isinstance(spec.get('version'), str) or spec['version'].lstrip('v') != version or
            (image is not None and (not isinstance(image, str) or not image.endswith('/alertmanager:v' + version)))):
        raise OperationError('ops.monitoring.email_incompatible')
    credential = client.secret_metadata(namespace, secret, key)
    existing = client.get(CONFIG, desired['metadata']['name'], namespace, optional=True)
    if existing is not None and any(existing.get('metadata', {}).get('labels', {}).get(k) != v
                                    for k, v in desired['metadata']['labels'].items()):
        raise OperationError('ops.change.foreign_resource')
    return {'manager': {'uid': meta['uid'], 'specHash': fingerprint(spec)},
            'credential': credential, 'config': publication._identity(existing)}


def publish(client, desired, manager, version, secret, key, expected):
    if capture(client, desired, manager, version, secret, key) != expected:
        raise OperationError('ops.change.stale')
    meta = desired['metadata']
    existing = client.get(CONFIG, meta['name'], meta['namespace'], optional=True)
    if publication._identity(existing) != expected['config']:
        raise OperationError('ops.change.stale')
    value = copy.deepcopy(desired if existing is None else existing)
    value['spec'] = copy.deepcopy(desired['spec'])
    value['metadata'].setdefault('labels', {}).update(meta['labels'])
    value['metadata'].setdefault('annotations', {})[publication.SPEC_HASH] = fingerprint(value['spec'])
    result = client.create(value) if existing is None else client.replace(value)
    if result is None:
        raise OperationError('ops.change.stale')
    after = capture(client, desired, manager, version, secret, key)
    if (after['manager'] != expected['manager'] or after['credential'] != expected['credential'] or
            after['config'] is None or after['config']['uid'] != result['metadata']['uid'] or
            after['config']['specHash'] != fingerprint(desired['spec'])):
        raise OperationError('ops.change.stale')
    return {'state': 'resource-written', 'config': after['config'], 'delivery': 'unverified'}
