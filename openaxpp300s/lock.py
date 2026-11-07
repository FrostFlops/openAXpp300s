"""Cross-process locks: one around each use of the serial port, one around each settings change, and small ones
around read-modify-writes of the state files.

A COM port has one owner at a time, and the host must keep one frame in flight (a setting frame sent while a status
reply is going out gets its ACK inserted into the reply). Every command therefore holds the port lock only while it
talks to the supply (a few frames, tens of ms) and closes the port again, so a background log and a foreground `set`
share the supply: the one that comes second waits for the lock (default up to 5 s). The settings lock
('settings_<port>') makes the checks of a change and its frames atomic across processes. msvcrt on Windows, flock
elsewhere.
"""
import os
import re
import time

from .errors import PP300SError
from .paths import ensure, state_dir


def lock_path(port):
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', str(port))
    return os.path.join(ensure(os.path.join(state_dir(), 'locks')), 'port_%s.lock' % safe)


def _lock(fh, on):
    """Non-blocking lock / unlock of byte 0; OSError when it is held."""
    if os.name == 'nt':
        import msvcrt
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK if on else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), (fcntl.LOCK_EX | fcntl.LOCK_NB) if on else fcntl.LOCK_UN)


class PortLock:
    def __init__(self, port, wait=5.0):
        self.port = port
        self.wait = wait
        self.fh = None

    def acquire(self):
        path = lock_path(self.port)
        t_end = time.monotonic() + max(0.0, self.wait)
        while True:
            fh = open(path, 'a+')
            try:
                _lock(fh, True)
                self.fh = fh
                return self
            except OSError:
                fh.close()
                if time.monotonic() >= t_end:
                    raise PP300SError('BUSY', 'another openaxpp300s command kept %s busy for more than %g s'
                                      % (self.port, self.wait),
                                      'retry; raise --lock-wait if a slow command (selftest) is running')
                time.sleep(0.005)

    def release(self):
        if self.fh:
            try:
                _lock(self.fh, False)
            except OSError:
                pass
            self.fh.close()
            self.fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
