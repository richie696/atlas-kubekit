# LB installer regression checks

Date: 2026-10-22

Scope: phase-two LB orchestration, `setup-lb.sh`, and the control-plane join
parameters required to match HAProxy backends. These checks repair and review
the existing installer; they do not deploy or restart the real LB pair.

## Corrected behavior

| Risk | Required behavior | Regression evidence |
| --- | --- | --- |
| Failed `systemctl enable` was ignored | Both services must be enabled; fail before replacing configuration otherwise | Enable and is-enabled failure injection |
| `set -e` bypassed restart rollback | EXIT handles restart, write, installed-config validation, listener and TERM failures | Restore managed config/state/sysctl files, kernel setting and previous service state |
| Initial failure had no previous config | Remove newly created managed files and stop services started by this run | Fresh-install restart failure |
| Same health-check content had unsafe metadata | Reinstall to repair owner/group/mode; normalize config and state permissions on rerun | Matching script with wrong mode, permissive config/state modes |
| Keepalived validation could fail after HAProxy had been changed | Validate both candidates before committing either configuration | Failed candidate leaves original files and service execution intact |
| First requested application VIP looked like inconsistent old state | Validate persisted state against old configuration before appending the requested VIP | First app VIP, rerun with same VIP, additional VIP |
| Corrupt manager markers/state could lose routing information | Reject reversed or incomplete markers and missing route/VIP blocks before replacement | Reversed markers and inconsistent persisted state |
| Completed deployment removed the saved VRRP secret | Read the existing named VRRP instance on all LBs; reuse its shared password and reject disagreement | Fresh generation, reuse, partial deployment, mismatched and invalid passwords; no secret in errors |
| ARP could pass when both registered LBs held the VIP | Require the same single registered holder on three consecutive observations, with bounded retries | Stable owner, election convergence, dual owners, no owner, flapping and unreachable node |
| Custom API port only reached cp1 and HAProxy | Append the selected API bind port and registered advertise address when another control plane joins | Control-plane custom-port argument test; worker arguments unchanged |
| Invalid node/VIP/backend/port inputs caused late failures | Reject before APT or configuration writes | Invalid IPv4, backend, interface and API/stats port conflict |
| Rerun restarted unchanged services or lost routes | Preserve managed routes/VIPs and avoid restarts when config bytes and service state match | Rerun and route-preservation test |
| Check-only could accidentally apply changes | No package install, enable, restart or managed file write | Check-only file and command assertions |
| Standby could not bind an application VIP it did not yet own | Persist and apply `net.ipv4.ip_nonlocal_bind=1`; restore the previous kernel value/file on failure | Initial application setting and failed sysctl/config/start rollback; live listener/failover and reboot checks |

## Commands and evidence boundary

```bash
python3 -m unittest discover -s tests -p test_lb_setup.py -v
bash -n 02-deploy-cluster.sh scripts/stages/setup-lb.sh scripts/stages/join-nodes.sh
git diff --check
```

Local result: all 13 test methods passed, including their parameterized failure
scenarios. Bash syntax, Python parsing, locale JSON and newly added translation
keys/formatting passed. ShellCheck was unavailable in the local environment.

The regression suite runs the actual shell and embedded Python against a
temporary filesystem. APT, systemd, root identity/ownership reporting,
HAProxy/Keepalived and network probes are controlled command doubles. Files,
permissions, snapshots, restoration and state merges execute in that temporary
filesystem. No production service or VIP is changed by these tests.

These tests establish command ordering, failure propagation, configuration
preservation and orchestration decisions. They do **not** establish real APT
installation, daemon syntax/version compatibility, script security on an actual
Ubuntu filesystem, startup after reboot, real VRRP convergence or failover, or
Kubernetes join/API readiness. Those require an isolated deployment using the
updated package, followed by live checks.

The join flags are documented in the upstream
[kubeadm join reference](https://kubernetes.io/docs/reference/setup-tools/kubeadm/kubeadm-join/).

Package installation and secure health-check installation may persist after a
later failure. Once configuration commit starts, configuration/state snapshots
are saved under `/var/backups/atlas-kubekit/lb-setup/run.*`; `paths` maps each
numbered snapshot to its original path; `services` records the previous active
and boot-enabled states. Rollback restores those service states as well as the
files. Rollback failures are reported with the
retained backup path. Power loss and SIGKILL cannot execute the EXIT handler.

The existing unauthenticated HAProxy stats listener on TCP 7000 still requires
management-network access controls before production exposure; the network
policy is a deployment choice and is not inferred by this bug fix.
