"""Run native first install against one frozen material identity and lab markers."""

import json
from pathlib import Path
import re

from . import transport


SOURCE = "$HOME/atlas030-e2e-source"


def phase_one(lab, node, output):
    transport.guard(lab, node)
    messages = json.loads((Path(__file__).resolve().parents[2] / "locales/en.json").read_text())[
        "messages"
    ]
    answers = {
        "phase01.prompt.hostname": node.name,
        "phase01.prompt.interface": node.interface,
        "phase01.prompt.network_mode": "keep",
        "phase01.prompt.current_ip": node.address,
        "phase01.prompt.ssh_user": "ubuntu",
        "phase01.prompt.admin_key_source": "paste",
        "phase01.message.key_prompt": lab.public_key,
        "phase01.prompt.confirm": "yes",
        "storage.prompt.data_disk_phase1": "none",
    }
    prompts = []
    for key, answer in answers.items():
        pattern = re.escape(messages[key]).replace("%s", "[^\\r\\n]*")
        if key not in ("phase01.message.key_prompt", "storage.prompt.data_disk_phase1"):
            pattern += r" \[[^\r\n]*\]: "
        prompts.append((key, re.compile(pattern), answer))
    code, answered = transport.interactive(
        lab,
        node,
        'sudo -k -n bash "' + SOURCE + '/01-prepare-node.sh" --lang en',
        prompts,
        output / ("phase1-" + node.name + ".log"),
        1800,
    )
    if code not in (0, 255) or "phase01.prompt.confirm" not in answered:
        raise RuntimeError("Phase one failed: " + node.name)
    return dict(node=node.name, exitCode=code, answered=answered, rebootVerificationRequired=True)


def deployment(lab, output, repeat=False):
    codes = {}
    for node in lab.nodes:
        transport.guard(lab, node)
        actual = transport.run(
            lab,
            node,
            "sudo -k -n python3 /usr/local/lib/k8s-deploy/scripts/operations/materials/runtime.py id",
        )
        if actual != lab.material_id:
            raise RuntimeError("Mixed material identities")
        if transport.run(lab, node, "hostname -s") != node.name:
            raise RuntimeError("Hostname mismatch")
        transport.run(lab, node, "sudo -n test -f /var/lib/k8s-deploy/ready")
    first = next(node for node in lab.nodes if node.name == "lb1")
    if not repeat:
        for node in lab.nodes:
            if node == first:
                continue
            try:
                peer = transport.run(
                    lab,
                    first,
                    'ssh -i "$HOME/.ssh/id_ed25519" -o IdentitiesOnly=yes -o BatchMode=yes '
                    "-o StrictHostKeyChecking=yes -o ConnectTimeout=5 ubuntu@"
                    + node.address
                    + " hostname -s",
                )
                if peer == node.name:
                    continue
            except RuntimeError:
                pass
            transport.run(
                lab,
                node,
                'sudo -n bash "' + SOURCE + '/01-prepare-node.sh" --lang en --enable-enrollment',
            )
            code = transport.run(
                lab, node, 'sudo -n bash "' + SOURCE + '/01-prepare-node.sh" --lang en --code'
            )
            if not re.fullmatch(r"[0-9A-Z]{4}(?:-[0-9A-Z]{4}){3}", code):
                raise RuntimeError("Invalid pairing response")
            codes[node.name] = code
    prompts = [
        (name, re.compile(r"Pairing code for " + name + r" \([^\r\n]+\), input hidden: "), code)
        for name, code in codes.items()
    ]
    prompts.append(("confirmed", re.compile(r"DEPLOY ALL[^\r\n]*\[NO\]: "), "DEPLOY ALL"))
    code, answered = transport.interactive(
        lab,
        first,
        'sudo -n bash "'
        + SOURCE
        + '/02-deploy-cluster.sh" --lang en --config "'
        + SOURCE
        + '/lab-cluster.json"',
        prompts,
        output / ("repeat.log" if repeat else "deploy.log"),
        5400,
    )
    if code:
        raise RuntimeError("Native deployment failed; inspect private log")
    return dict(
        exitCode=code,
        paired=[name for name in answered if name != "confirmed"],
        confirmed="confirmed" in answered,
        materialId=lab.material_id,
    )
