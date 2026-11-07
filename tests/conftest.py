"""Test isolation: every test gets its own home folder and the virtual supply, and real serial ports are refused.

A test must never reach a real PP300S: OPENAXPP300S_SIM=1 selects the simulator, OPENAXPP300S_NO_SERIAL=1 makes any
real port refuse to open, OPENAXPP300S_SIM_DELAY=0 removes the simulated latency. If you add an environment variable
that changes behaviour, set it here.
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAXPP300S_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('OPENAXPP300S_SIM', '1')
    monkeypatch.setenv('OPENAXPP300S_NO_SERIAL', '1')
    monkeypatch.setenv('OPENAXPP300S_SIM_DELAY', '0')
    monkeypatch.delenv('OPENAXPP300S_PORT', raising=False)
    from openaxpp300s import transport
    monkeypatch.setattr(transport, '_SIM', [False])
    from openaxpp300s import sim
    sim.save(sim.default_state())
    yield tmp_path


def run_cli(*args, capsys=None):
    """Run the CLI in-process -> (exit code, list of JSON docs printed)."""
    from openaxpp300s.cli import main
    code = main(list(args))
    out = capsys.readouterr().out if capsys is not None else ''
    docs = [json.loads(line) for line in out.splitlines() if line.startswith('{')]
    return code, docs


def run_cli_proc(*args, env=None, timeout=60):
    """Run the CLI in a child process (like the MCP server does) -> (exit code, docs, stderr)."""
    p = subprocess.run([sys.executable, '-m', 'openaxpp300s'] + list(args), cwd=ROOT, capture_output=True,
                       text=True, encoding='utf-8', env=env or dict(os.environ), timeout=timeout)
    docs = [json.loads(line) for line in p.stdout.splitlines() if line.startswith('{')]
    return p.returncode, docs, p.stderr


@pytest.fixture
def cli(capsys):
    def _run(*args):
        return run_cli(*args, capsys=capsys)
    return _run


@pytest.fixture
def simstate():
    """Read / change the virtual supply: simstate() -> dict; simstate(key=value, ...) sets keys."""
    from openaxpp300s import sim

    def _st(**kw):
        st = sim.load()
        if kw:
            st.update(kw)
            sim.save(st)
        return st
    return _st
