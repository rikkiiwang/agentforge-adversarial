from __future__ import annotations

from pathlib import Path

import yaml

from agentforge_adversarial.models import EvalCase


def load_cases(path: Path) -> list[EvalCase]:
    """Load EvalCases from a YAML file or recursively from a directory.

    Subdirectories of `evals/cases/` (e.g. `garak/`, `jailbreakbench/`,
    `houyi/`) hold imports from external attack libraries; all of them
    contribute to the seed pool in one campaign.
    """
    files = sorted(path.rglob("*.yaml")) if path.is_dir() else [path]
    return [EvalCase.model_validate(yaml.safe_load(f.read_text())) for f in files]
