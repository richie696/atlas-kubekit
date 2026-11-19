# SPDX-License-Identifier: MIT
"""Rules for Longhorn 1.13's labelled volume robustness metrics."""
from .storage_rules import pvc_capacity_rules


def volume_robustness_rules(namespace, release, claims, hold_seconds, max_age_seconds, severity, summary, description):
    # Reuse the PVC identity and common rule parameter validation.
    result = pvc_capacity_rules(namespace, release, claims, 15, hold_seconds, max_age_seconds,
                                severity, summary, description)
    result["metadata"]["name"] = "atlas-longhorn-robustness"
    group = result["spec"]["groups"][0]
    group["name"] = "atlas.storage.longhorn"
    for rule in group["rules"]:
        labels = rule["labels"]
        claim = rule["annotations"]["persistentvolumeclaim"]
        selector = 'pvc_namespace="' + labels["namespace"] + '",pvc="' + claim + '"'
        metric = 'longhorn_volume_robustness{' + selector
        # Longhorn Manager replicas all expose the same cluster-wide volume
        # metrics. Filter stale samples before aggregation so timestamp() still
        # observes source sample age, then collapse replica labels by volume/state.
        def fresh(state_selector):
            source = metric + state_selector + '}'
            age = '(time() - timestamp(' + source + '))'
            return ('max by (pvc_namespace, pvc, volume, state) ((' + source +
                    ') and (' + age + ' <= ' + str(max_age_seconds) + ') and (' + age + ' >= -10))')

        unknown_states = fresh(',state!~"healthy|degraded|faulted|unknown"')
        terms = ['(count(' + unknown_states + ') > 0)']
        healthy = fresh(',state="healthy"')
        for state in ("healthy", "degraded", "faulted", "unknown"):
            sample = fresh(',state="' + state + '"')
            valid = '((' + sample + ' == 0 or ' + sample + ' == 1))'
            terms.extend(('(count(' + sample + ') != 1)', 'absent(' + sample + ')',
                          '(count(' + sample + ') != count(' + valid + '))', 'absent(' + valid + ')',
                          '(sum(' + valid + ') != ' + ('1' if state == 'healthy' else '0') + ')'))
            if state != "healthy":
                terms.append('(count(' + sample + ' unless ignoring(state) ' + healthy + ') > 0)')
        rule["alert"] = "AtlasLonghornVolumeRobustnessOrTelemetryMissing"
        rule["expr"] = 'max(' + ' or '.join(terms) + ')'
        labels["risk_id"] = "R-STOR-03"
    return result
