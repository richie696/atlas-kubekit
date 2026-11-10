# SPDX-License-Identifier: MIT
"""Bound archive parsing before tarfile allocates extended headers or member lists."""
from contextlib import contextmanager
import gzip
import tarfile


class ArchiveLimitError(RuntimeError):
    pass


class ArchiveFormatError(ValueError):
    pass


class _MetadataReader:
    def __init__(self,source):
        self.source=source
        self.indexing=True
        self.remaining=8*1024*1024

    def read(self,size=-1):
        if size<0 or size>1024*1024:
            raise ArchiveLimitError('Archive metadata read limit exceeded')
        result=self.source.read(size)
        if self.indexing:
            self.remaining-=len(result)
            if self.remaining<0:raise ArchiveLimitError('Archive metadata limit exceeded')
        return result

    def seek(self,*args):return self.source.seek(*args)

    def tell(self):return self.source.tell()


class _LimitedReader:
    def __init__(self, source, limit):
        self.source, self.remaining = source, limit

    def read(self, size=-1):
        size = min(size, self.remaining + 1) if size >= 0 else self.remaining + 1
        result = self.source.read(size)
        self.remaining -= len(result)
        if self.remaining < 0:
            raise ArchiveLimitError("Archive byte limit exceeded")
        return result


@contextmanager
def gzip_tar(source, limit):
    with gzip.GzipFile(fileobj=source) as expanded:
        reader = _LimitedReader(expanded, limit)
        with tarfile.open(fileobj=reader, mode="r|") as archive:
            yield archive
        # A successful parse also validates the gzip trailer and bounded padding.
        while reader.read(1024 * 1024):
            pass


@contextmanager
def indexed_tar(path, limit, max_members=20000):
    """Native OCI exports use raw tar; bound IO without tarfile implementation hooks."""
    try:
        with open(path,'rb') as source:
            reader=_MetadataReader(source)
            with tarfile.open(fileobj=reader,mode='r:') as archive:
                total=0
                for index,member in enumerate(archive):
                    total+=member.size
                    if index>=max_members or member.size<0 or total>limit:
                        raise ArchiveLimitError('Archive index limit exceeded')
                reader.indexing=False
                yield archive
    except (tarfile.TarError, EOFError, OSError,RecursionError):
        raise ArchiveFormatError("Invalid archive") from None
