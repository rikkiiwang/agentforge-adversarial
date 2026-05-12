from __future__ import annotations

from openai import AsyncOpenAI


def make_openai(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key)


JUDGE_MODEL = "gpt-4o-mini"
MUTATOR_MODEL = "gpt-4o-mini"
