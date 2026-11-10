"""Select the supported Bash consistently on Linux and both macOS architectures."""
from pathlib import Path
import shutil
import subprocess


def compatible_bash():
    candidates = (shutil.which('bash'), '/opt/homebrew/bin/bash', '/usr/local/bin/bash', '/bin/bash')
    for candidate in dict.fromkeys(path for path in candidates if path):
        if not Path(candidate).is_file():
            continue
        result = subprocess.run([candidate, '-c', 'test "${BASH_VERSINFO[0]}" -ge 4'], capture_output=True)
        if result.returncode == 0:
            return candidate
    raise RuntimeError('Tests require Bash 4 or newer; install/select a supported Bash first')


BASH = compatible_bash()
