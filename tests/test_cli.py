from __future__ import annotations

import subprocess
import sys


def test_cli_help_runs():
    result = subprocess.run(
        [sys.executable, "-m", "agentforge_adversarial", "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "AgentForge adversarial CLI" in result.stdout
