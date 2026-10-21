"""Regression checks for phase-one reboot runtime and SSH setting checks."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
BASH = '/opt/homebrew/bin/bash' if Path('/opt/homebrew/bin/bash').exists() else 'bash'


class PrepareResumeTests(unittest.TestCase):
    def test_installed_copy_loads_catalog_without_source_tree(self):
        source = (REPO / '01-prepare-node.sh').read_text()
        function = source[source.index('persist_i18n_runtime() {'):source.index('start_enrollment() {')]
        with tempfile.TemporaryDirectory(prefix='atlas-prepare-test-') as d:
            root = Path(d); target = root / 'installed'; target.mkdir()
            bindir = root / 'bin'; bindir.mkdir()
            shim = bindir / 'readlink'
            shim.write_text('#!/usr/bin/env python3\nimport os,sys\nprint(os.path.realpath(sys.argv[-1]))\n')
            shim.chmod(0o755)
            command = 'set -euo pipefail\nLIB_DIR=' + shlex.quote(str(target)) + '\nSCRIPT_DIR=' + shlex.quote(str(REPO))
            command += '\nI18N_FILE="$SCRIPT_DIR/scripts/lib/i18n.sh"\n' + function + '\npersist_i18n_runtime\n'
            env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
            env.pop('ATLAS_LOCALES_DIR', None); env.pop('ATLAS_LANG', None)
            subprocess.run([BASH, '-c', command], env=env, check=True)
            shutil.copy(REPO / '01-prepare-node.sh', target / '01-prepare-node.sh')
            result = subprocess.run([BASH, str(target / '01-prepare-node.sh'), '--lang', 'zh_cn', '--help'],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('command not found', result.stderr)
            self.assertIn('用法：', result.stdout)

    def test_large_sshd_output_does_not_make_matching_settings_fail(self):
        source = (REPO / '01-prepare-node.sh').read_text()
        fragment = source[source.index('SSHD_EFFECTIVE="$(sshd -T)"'):source.index('systemctl reload ssh')]
        command = '''set -euo pipefail
sshd() { python3 -c 'print("passwordauthentication no\\nkbdinteractiveauthentication no\\n"+"extra setting\\n"*200000)'; }
die() { exit 1; }
''' + fragment
        subprocess.run([BASH, '-c', command], check=True)


if __name__ == '__main__': unittest.main()
