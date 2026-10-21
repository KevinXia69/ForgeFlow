from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_typography.py"
if not SCRIPT.is_file():
    pytest.skip("typography audit is local-runtime only", allow_module_level=True)


def test_no_sub_14px_readable_text() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_typography.py")],
        cwd=ROOT, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
