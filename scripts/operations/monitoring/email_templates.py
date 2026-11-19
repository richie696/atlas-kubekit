# SPDX-License-Identifier: MIT
"""Build localized Alertmanager email templates with actionable alert details."""
from scripts.lib.i18n import available_locales, translate
from .channels.alertmanager import ALERT_NAME_KEYS


def templates(lang):
    if lang not in available_locales():
        raise ValueError('unsupported notification language')
    label = lambda key: translate(lang, 'ops.monitoring.mail_' + key)
    firing, resolved = label('firing'), label('resolved')
    state = '{{ if eq .Status "firing" }}' + firing + '{{ else }}' + resolved + '{{ end }}'
    alert_rule = '{{ .CommonLabels.alertname }}'
    for name, key in reversed(tuple(ALERT_NAME_KEYS.items())):
        alert_rule = '{{ if eq .CommonLabels.alertname "' + name + '" }}' + label('alert_rule_' + key) + '{{ else }}' + alert_rule + '{{ end }}'
    common_severity = ('{{ if eq .CommonLabels.severity "critical" }}' + label('critical') +
                       '{{ else if eq .CommonLabels.severity "warning" }}' + label('warning') +
                       '{{ else if eq .CommonLabels.severity "info" }}' + label('info') +
                       '{{ else }}{{ .CommonLabels.severity }}{{ end }}')
    subject = ('[' + state + '] {{ len .Alerts }} ' + label('alerts') +
               '{{ if .CommonLabels.severity }}（' + label('severity') + ': ' + common_severity + '）{{ end }}：' +
               '{{ if .CommonAnnotations.summary }}{{ .CommonAnnotations.summary }}' +
               '{{ else if .CommonLabels.alertname }}' + alert_rule +
               '{{ else }}' + label('no_summary') + '{{ end }}')
    alert_severity = ('{{ if eq .Labels.severity "critical" }}' + label('critical') +
                      '{{ else if eq .Labels.severity "warning" }}' + label('warning') +
                      '{{ else if eq .Labels.severity "info" }}' + label('info') +
                      '{{ else }}{{ .Labels.severity }}{{ end }}')
    alert_rule = '{{ .Labels.alertname }}'
    for name, key in reversed(tuple(ALERT_NAME_KEYS.items())):
        alert_rule = '{{ if eq .Labels.alertname "' + name + '" }}' + label('alert_rule_' + key) + '{{ else }}' + alert_rule + '{{ end }}'
    lines = [label('status') + ': ' + state,
             label('name') + ': ' + alert_rule,
             '{{ if .Labels.severity }}' + label('severity') + ': ' + alert_severity + '{{ end }}',
             '{{ if .Annotations.summary }}' + label('summary') + ': {{ .Annotations.summary }}{{ else }}' +
             label('summary') + ': ' + label('no_summary') + '{{ end }}',
             '{{ if .Annotations.description }}' + label('description') + ': {{ .Annotations.description }}{{ else }}' +
             label('description') + ': ' + label('no_description') + '{{ end }}']
    for field in ('cluster', 'namespace', 'node', 'disk', 'pod', 'container', 'reason',
                  'horizontalpodautoscaler', 'deployment', 'statefulset', 'daemonset',
                  'pvc', 'service', 'job', 'instance', 'registered_members', 'quorum_required'):
        if field == 'pvc':
            lines.append('{{ if .Annotations.persistentvolumeclaim }}' + label('pvc') +
                         ': {{ .Annotations.persistentvolumeclaim }}{{ else if .Labels.persistentvolumeclaim }}' +
                         label('pvc') + ': {{ .Labels.persistentvolumeclaim }}{{ end }}')
            continue
        label_field = {'pvc': 'persistentvolumeclaim'}.get(field, field)
        label_key = field
        label_key = {'registered_members': 'etcd_members', 'quorum_required': 'etcd_quorum',
                     'horizontalpodautoscaler': 'hpa'}.get(field, label_key)
        lines.append('{{ if .Labels.' + label_field + ' }}' + label(label_key) + ': {{ .Labels.' + label_field + ' }}{{ end }}')
    lines.extend(('{{ if .Labels.risk_id }}' + label('risk_id') + ': {{ .Labels.risk_id }}{{ end }}',
                  '{{ if .Annotations.observed_value }}' + label('observed_value') +
                  ': {{ .Annotations.observed_value }}{{ end }}'))
    lines.extend((label('fingerprint') + ': {{ .Fingerprint }}',
                  label('start') + ': {{ .StartsAt }}',
                  '{{ if eq .Status "resolved" }}' + label('end') + ': {{ .EndsAt }}{{ end }}',
                  '{{ if .GeneratorURL }}' + label('source') + ': {{ .GeneratorURL }}{{ end }}'))
    status_color = '{{ if eq .Status "firing" }}#c94f3d{{ else }}#67a83d{{ end }}'
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body bgcolor="#f3f4f6" style="margin:0;padding:24px 12px;background-color:#f3f4f6;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif;color:#242424">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">'
        '<tr><td align="center">'
        '<table role="presentation" width="640" cellspacing="0" cellpadding="0" border="0" '
        'bgcolor="#ffffff" style="width:100%;max-width:640px;background-color:#ffffff;border:1px solid #dddddd;'
        'border-radius:8px;overflow:hidden">'
        '<tr><td bgcolor="' + status_color + '" style="padding:24px 28px;background-color:' + status_color +
        ';color:#ffffff;font-size:20px;font-weight:600">{{ len .Alerts }} ' + label('alerts') +
        ' · ' + state +
        '{{ if .CommonAnnotations.summary }}<div style="margin-top:10px;font-size:16px;font-weight:400">'
        '{{ .CommonAnnotations.summary }}</div>{{ else if .CommonLabels.alertname }}'
        '<div style="margin-top:10px;font-size:16px;font-weight:400">' + alert_rule + '</div>'
        '{{ end }}</td></tr>'
        '<tr><td style="padding:24px 28px">'
        '<p style="margin:0 0 20px;color:#666666;font-size:14px">' + label('receiver') + ': {{ .Receiver }}</p>'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr>'
        '<td bgcolor="#348bd4" style="background-color:#348bd4;border-radius:6px">'
        '<a href="{{ .ExternalURL }}" style="display:inline-block;padding:14px 22px;'
        'background-color:#348bd4;border-radius:6px;color:#ffffff;text-decoration:none;'
        'font-size:16px;font-weight:600">' + label('view_in_alertmanager') + '</a>'
        '</td></tr></table>'
        '{{ range .Alerts }}'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
        'style="margin-top:24px;border:1px solid #e3e5e8;border-radius:6px">'
        '<tr><td style="padding:18px 20px 8px;font-size:18px;font-weight:600">' + alert_rule + '</td></tr>'
        '<tr><td style="padding:0 20px 14px;font-size:15px">'
        '{{ if .Annotations.summary }}<strong>' + label('summary') + ':</strong> {{ .Annotations.summary }}'
        '{{ else }}<strong>' + label('summary') + ':</strong> ' + label('no_summary') + '{{ end }}'
        '</td></tr>'
        '<tr><td style="padding:0 20px 18px;font-size:14px;color:#555555">'
        '{{ if .Annotations.description }}<strong>' + label('description') + ':</strong> {{ .Annotations.description }}'
        '{{ else }}<strong>' + label('description') + ':</strong> ' + label('no_description') + '{{ end }}'
        '</td></tr>'
        '<tr><td bgcolor="#f7f8fa" style="padding:16px 20px;background-color:#f7f8fa;border-top:1px solid #e3e5e8;'
        'font-size:13px;line-height:1.8">' +
        ''.join('<div style="margin:0 0 5px">' + line + '</div>' for line in lines) +
        '</td></tr>'
        '<tr><td style="padding:12px 20px;font-size:13px"><a href="{{ .GeneratorURL }}" '
        'style="color:#348bd4">' + label('source') + '</a></td></tr></table>{{ end }}'
        '</td></tr>'
        '<tr><td style="padding:18px 24px;text-align:center;border-top:1px solid #eeeeee;'
        'font-size:12px;color:#888888">' + label('notification_title') + '</td></tr>'
        '</table></td></tr></table></body></html>'
    )
    return {'subject': subject, 'html': html}
