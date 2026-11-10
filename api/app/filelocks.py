"""Exclusive cross-process file locks that work on Linux and on Windows coordinators."""

import contextlib
import os
import time


@contextlib.contextmanager
def file_lock(handle, *, blocking=True):
    """Yield True once the lock is held, or False when ``blocking`` is off and it is taken."""
    if os.name == "nt":
        import msvcrt

        # msvcrt locks a byte range, so the file needs at least one byte.
        if os.fstat(handle.fileno()).st_size == 0:
            os.write(handle.fileno(), b"\0")
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if not blocking:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
