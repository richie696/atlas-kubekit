"""The phase-one storage payload really runs via python -c without __file__."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]


class EmbeddedStorageTests(unittest.TestCase):
    def test_embedded_message_resolution_without_file_in_all_locales(self):
        source=(ROOT/'scripts/stages/storage.py').read_text()
        for locale,name in [('en','en'),('zh_CN','zh_cn'),('zh_TW','zh_tw')]:
            program="__name__='embedded-test'\n"+source+"\nprint(tr('storage.prompt.data_disk_phase1','none'))\n"
            result=subprocess.run([sys.executable,'-c',program],capture_output=True,text=True,
                env={**os.environ,'ATLAS_LANG':locale,'ATLAS_LOCALES_DIR':str(ROOT/'locales')})
            self.assertEqual(result.returncode,0,result.stderr)
            expected=json.loads((ROOT/'locales'/(name+'.json')).read_text())['messages']['storage.prompt.data_disk_phase1']%'none'
            self.assertEqual(result.stdout.strip(),expected.strip())


if __name__=='__main__':unittest.main()
