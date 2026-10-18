import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows process containment')
def test_worker_can_create_its_child_cleanup_job():
    result = subprocess.run([sys.executable, '-c',
        "import os; from daemon import child_job; job=child_job(); print('JOB_READY',flush=True); os._exit(0)"],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=10)
    assert 'JOB_READY' in result.stdout, result.stderr
