"""A code-only upgrade must not make optional commands a daemon startup dependency."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BLOCK_ASTRONOMY = '''
import importlib.abc
import sys
class WithoutAstronomy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'astral'}:
            raise ModuleNotFoundError('simulated missing astronomy dependency: ' + fullname)
sys.meta_path.insert(0, WithoutAstronomy())
'''


@pytest.mark.parametrize('flag', ['--help', '--version'])
def test_cli_starts_without_astronomy_dependencies(flag):
    script = BLOCK_ASTRONOMY + f'''
from meshgarden.main import main
main([{flag!r}])
'''
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


@pytest.mark.parametrize('command', ['SUN'])
def test_missing_library_is_a_bounded_command_reply_not_a_crash(command):
    script = BLOCK_ASTRONOMY + f'''
from types import SimpleNamespace
from meshgarden.commands import Command
from meshgarden.main import Daemon
service = SimpleNamespace(timezone='America/Los_Angeles')
observatory = SimpleNamespace(latitude=37.77, longitude=-122.42)
daemon = SimpleNamespace(cfg=SimpleNamespace(service=service, observatory=observatory))
reply = Daemon._build_response_body(daemon, Command({command!r}), '!aabbccdd', 1700000000)
assert 'unavailable' in reply and len(reply.encode()) <= 120, reply
# Unrelated offline features stay available after the failed import.
assert 'MOON' in Daemon._build_response_body(daemon, Command.MOON, '!aabbccdd', 1700000000)
'''
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
