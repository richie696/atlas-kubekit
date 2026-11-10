"""Archive limits must apply before extended-header allocation and member indexing."""

import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.lib.archive import indexed_tar, gzip_tar, ArchiveLimitError
from scripts.operations.materials.verification import safe_tar
from scripts.operations.models import OperationError


class ArchiveTests(unittest.TestCase):
    def test_oversized_pax_header_is_refused_before_reading_its_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "bad.tar"
            item = tarfile.TarInfo("PaxHeader")
            item.type = tarfile.XHDTYPE
            item.size = 1024 * 1024 + 1
            file.write_bytes(item.tobuf())
            with self.assertRaises(ArchiveLimitError):
                with indexed_tar(file, 2 * 1024**3) as archive:
                    archive.getmembers()

    def test_header_count_is_bounded_while_index_is_built(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "many.tar"
            with tarfile.open(file, "w") as archive:
                for name in ("a", "b", "c"):
                    archive.addfile(tarfile.TarInfo(name))
            with self.assertRaises(ArchiveLimitError):
                with indexed_tar(file, 1024 * 1024, max_members=2) as archive:
                    archive.getmembers()

    def test_gzip_padding_and_trailer_are_checked_even_after_tar_eof(self):
        raw = gzip.compress(b"\0" * 10240 + b"x" * (128 * 1024))
        with self.assertRaises(ArchiveLimitError):
            with gzip_tar(io.BytesIO(raw), 32768) as archive:
                list(archive)
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "bad.tgz"
            file.write_bytes(raw)
            with self.assertRaises(OperationError):
                safe_tar(file, 32768, ("cilium/",), ())


if __name__ == "__main__":
    unittest.main()
