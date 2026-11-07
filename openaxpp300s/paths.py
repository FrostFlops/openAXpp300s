"""Where openaxpp300s keeps its files (state/ for locks, last values sent, limits, stop requests, simulator state;
data/ for logs).

A source checkout (pyproject.toml next to the package) keeps them in the checkout. An installed package uses
$OPENAXPP300S_HOME, else %LOCALAPPDATA%\\openaxpp300s on Windows and ~/.local/share/openaxpp300s elsewhere, so
nothing is written into site-packages.
"""
import os

PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def home():
    env = os.environ.get('OPENAXPP300S_HOME')
    if env:
        return os.path.abspath(env)
    if os.path.exists(os.path.join(PKG_PARENT, 'pyproject.toml')) and \
            os.path.isdir(os.path.join(PKG_PARENT, 'openaxpp300s')):
        return PKG_PARENT
    base = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, 'openaxpp300s')


def state_dir():
    return os.path.join(home(), 'state')


def data_dir():
    return os.path.join(home(), 'data')


def ensure(path):
    os.makedirs(path, exist_ok=True)
    return path
