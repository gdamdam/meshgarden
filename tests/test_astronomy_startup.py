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
from meshgarden.modules import ModuleRequest, build_registry
reg = build_registry(latitude=37.77, longitude=-122.42)
req = ModuleRequest('', '!aabbccdd', 1700000000, 120, False, False)
reply = reg.run(reg.lookup({command!r}), req)
assert 'unavailable' in reply and len(reply.encode()) <= 120, reply
# Unrelated offline features stay available after the failed import.
assert 'MOON' in reg.run(reg.lookup('MOON'), req)
'''
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
