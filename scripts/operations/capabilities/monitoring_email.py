# SPDX-License-Identifier: MIT
"""Plan and publish a single owned Alertmanager email route."""
import copy
from ..configuration import normalize, revision
from ..models import OperationError, fingerprint
from ..monitoring import alert_routing, email_publication


def _manifest(value):
    try:
        return email_publication.manifest(value['namespace'], value['release'], value['version'],
            value['smtpHost'], value['smtpPort'], value['sender'], value['recipient'], value['username'],
            value['secretName'], value['secretKey'], value['severity'], value['language'], value['routePolicy'])
    except (KeyError, TypeError, ValueError):
        raise OperationError('ops.change.invalid_parameters') from None


def parameters(value):
    fields = {'configuration', 'namespace', 'release', 'managerName', 'version', 'smtpHost', 'smtpPort',
              'sender', 'recipient', 'username', 'secretName', 'secretKey', 'severity', 'language',
              'routePolicy', 'expected'}
    if not isinstance(value, dict) or set(value) != fields or not isinstance(value['expected'], dict):
        raise OperationError('ops.change.invalid_parameters')
    result = copy.deepcopy(value)
    result['configuration'] = normalize(result['configuration'])
    try:
        policy = result['routePolicy']
        if not isinstance(policy, dict) or set(policy) != {
                'matchers', 'groupBy', 'groupWait', 'groupInterval', 'repeatInterval'}:
            raise ValueError('invalid route policy')
        result['routePolicy'] = alert_routing.route_policy(policy['matchers'], policy['groupBy'],
            policy['groupWait'], policy['groupInterval'], policy['repeatInterval'])
    except (AttributeError, TypeError, ValueError):
        raise OperationError('ops.change.invalid_parameters') from None
    _manifest(result)
    expected = result['expected']
    if set(expected) != {'manager', 'credential', 'config'}:
        raise OperationError('ops.change.invalid_parameters')
    for key, names in (('manager', {'uid', 'specHash'}), ('credential', {'uid', 'resourceVersion'}),
                       ('config', {'uid', 'resourceVersion', 'specHash'})):
        identity = expected[key]
        if key == 'config' and identity is None:
            continue
        if (not isinstance(identity, dict) or set(identity) != names or
                any(not isinstance(x, str) or not 1 <= len(x) <= 128 for x in identity.values())):
            raise OperationError('ops.change.invalid_parameters')
    return result


def from_arguments(args, config, environment):
    config, _ = environment.verify(config)
    values = {'configuration': config, 'namespace': args.monitoring_namespace, 'release': args.monitoring_release,
              'managerName': args.monitoring_alertmanager_name, 'version': args.monitoring_alertmanager_version,
              'smtpHost': args.monitoring_smtp_host, 'smtpPort': args.monitoring_smtp_port,
              'sender': args.monitoring_email_sender, 'recipient': args.monitoring_email_recipient,
              'username': args.monitoring_email_username, 'secretName': args.monitoring_email_secret,
              'secretKey': args.monitoring_email_secret_key, 'severity': args.monitoring_severity,
              'routePolicy': alert_routing.from_arguments(args),
              'language': args.monitoring_notification_language}
    values['expected'] = email_publication.capture(environment.client, _manifest(values), values['managerName'],
        values['version'], values['secretName'], values['secretKey'])
    return parameters(values)


def preview(value):
    value = parameters(value)
    return (('ops.monitoring.email_preview', (value['namespace'], value['managerName'], value['severity'],
            value['smtpHost'], value['smtpPort'], value['sender'], value['recipient'], value['secretName'], value['secretKey'])),
            ('ops.monitoring.route_policy_preview', (','.join(value['routePolicy']['matchers'][i]['name'] +
                value['routePolicy']['matchers'][i]['matchType'] + value['routePolicy']['matchers'][i]['value']
                for i in range(len(value['routePolicy']['matchers']))), ','.join(value['routePolicy']['groupBy']),
                value['routePolicy']['groupWait'], value['routePolicy']['groupInterval'],
                value['routePolicy']['repeatInterval'])),
            ('ops.monitoring.email_warning', ()))


def observe(value, environment):
    values = parameters(value)
    config, _ = environment.verify(values['configuration'])
    desired = _manifest(values)
    expected = email_publication.capture(environment.client, desired, values['managerName'], values['version'],
                                         values['secretName'], values['secretKey'])
    if expected != values['expected']:
        raise OperationError('ops.change.stale')
    return {'configurationRevision': revision(config), 'target': expected,
            'desiredSpecHash': fingerprint(desired['spec'])}


def apply(value, environment, checkpoint, run_id):
    values = parameters(value)
    environment.verify(values['configuration'])
    checkpoint('monitoring-email')
    return email_publication.publish(environment.client, _manifest(values), values['managerName'], values['version'],
                                     values['secretName'], values['secretKey'], values['expected'])
