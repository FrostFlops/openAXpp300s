"""Small JSON files under state/: the values this tool last sent, the bench limits, the port that answered last,
stop requests and the registry of running long commands (watch / log / sequence)."""
import json
import os
import time

from .paths import ensure, state_dir


def _path(*parts):
    return os.path.join(state_dir(), *parts)


class StateFileError(Exception):
    """A state file exists but cannot be read or parsed (after retries)."""


def read_json(path, default=None, strict=False):
    """The JSON in path; default when the file does not exist. A file being replaced by another process (Windows
    PermissionError) is retried; an unreadable or corrupt file gives default, or StateFileError when strict."""
    last = None
    for _ in range(40):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except PermissionError as e:
            last = e
            time.sleep(0.01)
        except (OSError, ValueError) as e:
            last = e
            break
    if strict:
        raise StateFileError('%s: %s' % (path, last))
    return default


def write_json(path, obj):
    ensure(os.path.dirname(path))
    tmp = '%s.%d.tmp' % (path, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    for _ in range(100):                     # Windows: the target may be open in another process for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.01)
    os.replace(tmp, path)


def _file_lock(name):
    from .lock import PortLock
    return PortLock('state_' + name, wait=10.0)


def port_key(port):
    """Key of a port in the state files: COM14 and com14 are the same port on Windows."""
    return str(port).upper() if os.name == 'nt' else str(port)


# ------------------------------------------------------------------ last values sent (the supply cannot report
# its settings, so this is the only record; the front panel may have changed them since)

def sent_path():
    return _path('last_sent.json')


def record_sent(port, key, value, raw, t=None):
    with _file_lock('last_sent'):
        try:
            d = read_json(sent_path(), {}, strict=True) or {}
        except StateFileError:
            os.replace(sent_path(), sent_path() + '.corrupt')     # keep it for inspection, start a fresh record
            d = {}
        d.setdefault(port_key(port), {})[key] = {'value': value, 'raw': raw, 't': round(t or time.time(), 3)}
        write_json(sent_path(), d)


def last_sent(port):
    return (read_json(sent_path(), {}) or {}).get(port_key(port), {})


# ------------------------------------------------------------------ bench limits (set by the user with the CLI)

def limits_path():
    return _path('limits.json')


def get_limits():
    """The bench limits. Fails closed: an existing but unreadable limits file raises UNSAFE_OPERATION."""
    from .errors import PP300SError
    try:
        d = read_json(limits_path(), {}, strict=True) or {}
    except StateFileError as e:
        raise PP300SError('UNSAFE_OPERATION', 'the bench limits file cannot be read: %s' % e,
                          'fix or remove %s (openaxpp300s limits --clear rewrites it)' % limits_path()) from None
    return {k: d.get(k) for k in ('max_voltage', 'max_current', 'note', 'updated')}


def set_limits(max_voltage=None, max_current=None, note=None, clear=False):
    with _file_lock('limits'):
        if clear:
            d = {}
        else:
            try:
                d = read_json(limits_path(), {}, strict=True) or {}
            except StateFileError:
                from .errors import PP300SError
                raise PP300SError('UNSAFE_OPERATION', 'the bench limits file cannot be read',
                                  'use --clear to rewrite it, then set the limits again') from None
        if max_voltage is not None:
            d['max_voltage'] = max_voltage
        if max_current is not None:
            d['max_current'] = max_current
        if note is not None:
            d['note'] = note
        d['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
        write_json(limits_path(), d)
    return get_limits()


# ------------------------------------------------------------------ port that answered last

def port_path():
    return _path('port.json')


def remember_port(port):
    write_json(port_path(), {'port': port, 't': round(time.time(), 3)})


def remembered_port():
    return (read_json(port_path(), {}) or {}).get('port')


# ------------------------------------------------------------------ running long commands and stop requests

def running_dir():
    return ensure(_path('running'))


def stop_dir():
    return ensure(_path('stop'))


def register_running(cmd, info):
    pid = os.getpid()
    write_json(os.path.join(running_dir(), '%d.json' % pid), dict(info, cmd=cmd, pid=pid, started=time.time()))
    clear_stop(pid)
    return pid


def unregister_running(pid=None):
    pid = pid or os.getpid()
    for p in (os.path.join(running_dir(), '%d.json' % pid), os.path.join(stop_dir(), str(pid))):
        try:
            os.remove(p)
        except OSError:
            pass


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259                     # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def list_running():
    out = []
    d = running_dir()
    for name in sorted(os.listdir(d)):
        if not name.endswith('.json'):
            continue
        info = read_json(os.path.join(d, name))
        if not info:
            continue
        if pid_alive(int(info.get('pid', 0))):
            out.append(info)
        else:
            unregister_running(int(info.get('pid', 0)))
    return out


def request_stop(pid=None, cmd=None):
    """Ask running watch/log/sequence commands to stop (all, one pid, or one command kind) -> those asked."""
    asked = []
    for info in list_running():
        if pid is not None and info['pid'] != pid:
            continue
        if cmd is not None and info['cmd'] != cmd:
            continue
        with open(os.path.join(stop_dir(), str(info['pid'])), 'w') as f:
            f.write(str(time.time()))
        asked.append(info)
    return asked


def stop_requested(pid=None):
    return os.path.exists(os.path.join(stop_dir(), str(pid or os.getpid())))


def clear_stop(pid=None):
    try:
        os.remove(os.path.join(stop_dir(), str(pid or os.getpid())))
    except OSError:
        pass
