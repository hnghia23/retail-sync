"""Mọi CLI in được tiếng Việt khi output bị chuyển hướng với encoding không phải UTF-8.

Tái hiện lỗi Windows (stdout chuyển hướng = cp1252) trên mọi hệ điều hành bằng cách ép
`PYTHONIOENCODING=cp1252`. Thiếu `utf8_stdio()` thì `--help` chết bằng UnicodeEncodeError.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIS = [
    "pipeline",
    "simulator",
    "edge.ops.seed",
    "central.ops.seed",
    "central.ops.provision_store",
    "central.ops.reconcile",
]


@pytest.mark.parametrize("module", CLIS)
def test_cli_help_survives_a_non_utf8_redirected_stdout(module: str) -> None:
    env = os.environ | {
        "PYTHONIOENCODING": "cp1252",
        "PYTHONUTF8": "0",
        "PYTHONPATH": str(ROOT / "packages"),
    }
    result = subprocess.run(  # noqa: S603 — lệnh cố định
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-1000:]
    assert "usage:" in result.stdout.decode("utf-8")
