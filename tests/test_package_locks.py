"""Wait on actual POSIX package locks without deleting locks or killing owners."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
from tests.shell import BASH


class PackageLocksTests(unittest.TestCase):
    def test_active_lock_times_out_and_released_lock_succeeds(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else None) as directory:
            file=Path(directory)/'lock'
            holder=subprocess.Popen(['python3','-c',
                'import fcntl,sys; f=open(sys.argv[1],"w"); fcntl.lockf(f,fcntl.LOCK_EX); print("ready",flush=True); sys.stdin.readline()',str(file)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(),'ready')
                script='source "$1"; atlas_wait_locks 1 waiting "$2"'
                args=[BASH,'-c',script,'test',str(ROOT/'scripts/lib/packages.sh'),str(file)]
                result=subprocess.run(args,capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,75);self.assertIn('waiting',result.stderr)
                self.assertTrue(file.exists())
                holder.stdin.write('\n');holder.stdin.flush();holder.wait(timeout=5)
                self.assertEqual(subprocess.run(args,capture_output=True,timeout=5).returncode,0)
            finally:
                if holder.poll() is None:holder.terminate();holder.wait(timeout=5)
                holder.stdin.close();holder.stdout.close()


if __name__=='__main__':unittest.main()
