from __future__ import annotations

import os

from openai import AsyncOpenAI


def make_openai(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key)


JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")
MUTATOR_MODEL = os.environ.get("MUTATOR_MODEL", "gpt-4o-mini")
