#!/usr/bin/env python3
"""Test native Alertmanager SMTP over implicit TLS using a local fake server."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.operations.monitoring.channels.email import receiver
from scripts.operations.monitoring.email_templates import templates
from tests.e2e.monitoring_alertmanager import post
from tests.e2e.monitoring_prometheus import cleanup_owned, docker

SERVER = '''import base64, socket, ssl
from email import policy
from email.parser import BytesParser
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('/test/server.crt','/test/server.key')
listener=socket.socket(); listener.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
listener.bind(('0.0.0.0',465)); listener.listen(5)
while True:
 conn,_=listener.accept()
 try:
  with ctx.wrap_socket(conn,server_side=True) as sock:
   stream=sock.makefile('rwb',buffering=0); stream.write(b'220 smtp test\\r\\n')
   in_data=False; authenticated=False; message=[]
   while True:
    line=stream.readline()
    if not line: break
    if in_data:
     if line == b'.\\r\\n':
      mail=BytesParser(policy=policy.default).parsebytes(b''.join(message))
      contents=' '.join(part.get_content() for part in mail.walk() if part.get_content_type() == 'text/html')
      html_parts=[part.get_content() for part in mail.walk() if part.get_content_type() == 'text/html']
      plain_parts=[part for part in mail.walk() if part.get_content_type() == 'text/plain']
      styled_html_ok=bool(html_parts) and all(marker in html_parts[0] for marker in
          ('bgcolor="#f3f4f6"','bgcolor="#348bd4"','在 Alertmanager 中查看'))
      is_resolved='告警恢复' in contents
      required=('告警规则: 节点未就绪或监控数据缺失',
                ('请确认该节点 Ready 状态和最新监控样本已恢复。' if is_resolved else
                 '请检查该节点的 kubelet 心跳和网络连通性'),
                '临时测试节点','警告','风险编号: R-NODE-01',
                ('观测值: Ready=1（测试注入）' if is_resolved else '观测值: Ready=0（测试注入）'))
      has_state=any(value in contents for value in ('告警触发','告警恢复'))
      summary_ok=any(value in contents for value in ('节点临时测试节点未就绪或就绪指标缺失','节点临时测试节点已恢复就绪'))
      subject_ok=any(value in str(mail['subject']) for value in ('节点临时测试节点未就绪或就绪指标缺失','节点临时测试节点已恢复就绪'))
      content_ok=has_state and summary_ok and subject_ok and styled_html_ok and not plain_parts and all(value in contents for value in required)
      print('CONTENT_OK' if content_ok else 'CONTENT_BAD state=%s summary=%s subject=%s html=%s details=%s' %
            (has_state, summary_ok, subject_ok, styled_html_ok, [value in contents for value in required]),flush=True)
      print('ACCEPTED',flush=True); stream.write(b'250 accepted\\r\\n'); in_data=False
     else: message.append(line)
     continue
    cmd=line.split(b' ',1)[0].upper().strip()
    if cmd in (b'EHLO',b'HELO'): stream.write(b'250-smtp\\r\\n250 AUTH PLAIN\\r\\n')
    elif cmd == b'AUTH':
     try: valid=base64.b64decode(line.split()[2],validate=True) == b'\\0sender@example.test\\0test-only-password'
     except (ValueError,IndexError): valid=False
     authenticated=valid
     stream.write(b'235 authenticated\\r\\n' if valid else b'535 invalid\\r\\n')
    elif cmd in (b'MAIL',b'RCPT'): stream.write(b'250 ok\\r\\n' if authenticated else b'530 auth required\\r\\n')
    elif cmd == b'DATA': stream.write(b'354 send data\\r\\n'); in_data=True
    elif cmd == b'QUIT': stream.write(b'221 bye\\r\\n'); break
    else: stream.write(b'500 unsupported\\r\\n')
 except (OSError,ssl.SSLError): pass
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--version", choices=("0.28.1", "0.31.1", "0.34.2", "0.35.0"), default="0.35.0")
    args = parser.parse_args()
    if args.confirm != "atlas-monitor-e2e":
        parser.error("Explicit confirmation atlas-monitor-e2e is required")
    def logs(container):
        result = docker('logs', container)
        return result.stdout + result.stderr
    suffix = uuid.uuid4().hex[:12]
    network = "atlas-smtp-e2e-" + suffix
    smtp, manager = "atlas-smtp-" + suffix, "atlas-mail-am-" + suffix
    try:
        temp_root = "/private/tmp" if Path("/private/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="atlas-mail-", dir=temp_root) as directory:
            root = Path(directory).resolve()
            (root / "smtp.py").write_text(SERVER)
            (root / "password").write_text("test-only-password")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(root / "server.key"), "-out", str(root / "server.crt"),
                            "-subj", "/CN=smtp", "-addext", "subjectAltName=DNS:smtp"],
                           check=True, capture_output=True, timeout=20)
            (root / "password").chmod(0o644)  # disposable fixture; Alertmanager runs unprivileged
            email = receiver("mail-test", args.version, "smtp", 465, "sender@example.test",
                             "receiver@example.test", "sender@example.test", "/test/password")
            template = templates('zh_CN')
            email['email_configs'][0].update(headers={'Subject': template['subject']},
                                               html=template['html'])
            email["email_configs"][0]["tls_config"] = {"ca_file": "/test/server.crt"}
            config = {"route": {"receiver": "mail-test", "group_wait": "1s", "group_interval": "2s"},
                      "receivers": [email]}
            (root / "alertmanager.json").write_text(json.dumps(config))
            docker("network", "create", "--label", "atlas-kubekit.e2e=" + suffix, network)
            docker("run", "-d", "--pull=never", "--name", smtp, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--network", network, "--network-alias", "smtp", "-v", str(root) + ":/test:ro",
                   "python:3.13-slim", "python", "-u", "/test/smtp.py")
            docker("run", "-d", "--pull=never", "--name", manager, "--label", "atlas-kubekit.e2e=" + suffix,
                   "--network", network, "-p", "127.0.0.1::9093", "-v", str(root) + ":/test:ro",
                   "prom/alertmanager:v" + args.version, "--config.file=/test/alertmanager.json", "--cluster.listen-address=")
            port = docker("port", manager, "9093/tcp").stdout.strip().rsplit(":", 1)[1]
            url = "http://127.0.0.1:" + port
            deadline = time.monotonic() + 25
            while True:
                try:
                    with urlopen(url + "/-/ready", timeout=2) as response:
                        if response.status == 200: break
                except OSError:
                    pass
                if time.monotonic() >= deadline: raise RuntimeError("Alertmanager failed to start")
                time.sleep(1)
            now = datetime.now(timezone.utc)
            post(url, now - timedelta(seconds=5), now + timedelta(minutes=5))
            deadline = time.monotonic() + 30
            while "ACCEPTED" not in logs(smtp):
                if time.monotonic() >= deadline:
                    smtp_log = logs(smtp)[-2000:]
                    manager_log = logs(manager)[-3000:]
                    raise RuntimeError('SMTP server did not accept test message; SMTP log=' + repr(smtp_log) +
                                       '; Alertmanager log=' + repr(manager_log))
                time.sleep(1)
            if 'CONTENT_OK' not in logs(smtp):
                raise RuntimeError('localized alert details missing from email')
            post(url, now - timedelta(seconds=5), now - timedelta(seconds=1))
            deadline = time.monotonic() + 30
            while logs(smtp).count('CONTENT_OK') < 2:
                if time.monotonic() >= deadline:
                    smtp_log = logs(smtp)[-2000:]
                    manager_log = logs(manager)[-2000:]
                    raise RuntimeError('SMTP server did not accept recovery notification; SMTP log=' +
                                       repr(smtp_log) + '; Alertmanager log=' + repr(manager_log))
                time.sleep(1)
            print("Alertmanager v" + args.version + " delivered to isolated SMTP over verified implicit TLS")
    finally:
        cleanup_owned(suffix, network, (manager, smtp))
        print("Owned temporary containers and network cleaned up.")


if __name__ == "__main__":
    main()
