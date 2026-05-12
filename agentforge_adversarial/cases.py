from __future__ import annotations

from pathlib import Path

import yaml

from agentforge_adversarial.models import EvalCase


def load_cases(path: Path) -> list[EvalCase]:
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    return [EvalCase.model_validate(yaml.safe_load(f.read_text())) for f in files]
