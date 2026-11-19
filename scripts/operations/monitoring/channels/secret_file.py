# SPDX-License-Identifier: MIT
"""Read a local single-line test credential without exposing its contents."""
import os
import stat


def read_private(path):
    """Require an owned, non-symlink regular file inaccessible to group/others."""
    if not isinstance(path, str) or not os.path.isabs(path) or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("invalid credential file")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 8192):
            raise ValueError()
        raw = os.read(fd, 8193)
        if not raw or len(raw) > 8192:
            raise ValueError()
        value = raw.decode("utf-8").removesuffix("\n")
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise ValueError("invalid credential file") from None
    finally:
        if fd is not None:
            os.close(fd)
