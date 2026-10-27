"""Pure Node folder-client regression; no browser, clipboard or user terminal."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


def test_folder_client_node_regressions():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node unavailable')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('folder-client.test.cjs'))],
                            capture_output=True, text=True, encoding='utf-8', timeout=30,
                            env=dict(os.environ, SMA_TEST_PYTHON=sys.executable))
    assert result.returncode == 0, result.stdout + result.stderr
